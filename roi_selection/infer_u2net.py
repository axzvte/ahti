#!/usr/bin/env python3
"""
U²-Net 单图显著性检测工具

输出：
1. *_saliency.png : 0~255 灰度显著性图
2. *_mask.png     : 二值重要区域掩膜
3. *_overlay.png  : 掩膜与原图叠加预览
4. *_region.json  : 图像尺寸、阈值、区域占比和外接矩形

使用前请克隆官方 U-2-Net 仓库，并下载 u2net.pth。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
import torch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run U²-Net saliency detection on one image.")
    parser.add_argument("--image", required=True, type=Path, help="输入图片路径")
    parser.add_argument(
        "--repo",
        default=Path("U-2-Net"),
        type=Path,
        help="官方 U-2-Net 仓库目录，默认：./U-2-Net",
    )
    parser.add_argument(
        "--weights",
        default=None,
        type=Path,
        help="u2net.pth 路径；不填写时使用仓库默认位置",
    )
    parser.add_argument(
        "--output",
        default=Path("output"),
        type=Path,
        help="输出目录，默认：./output",
    )
    parser.add_argument(
        "--threshold",
        default=0.5,
        type=float,
        help="二值化阈值，范围 0~1，默认：0.5",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="运行设备，默认自动选择",
    )
    return parser.parse_args()


def select_device(requested: str) -> torch.device:
    if requested == "cpu":
        return torch.device("cpu")

    if requested == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("指定了 CUDA，但 PyTorch 当前无法访问 GPU。")
        return torch.device("cuda")

    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model(repo_dir: Path, weights_path: Path, device: torch.device) -> torch.nn.Module:
    repo_dir = repo_dir.resolve()
    if not repo_dir.exists():
        raise FileNotFoundError(f"找不到 U-2-Net 仓库目录：{repo_dir}")

    sys.path.insert(0, str(repo_dir))
    try:
        from model import U2NET  # type: ignore
    except ImportError as exc:
        raise ImportError(
            f"无法从 {repo_dir} 导入 U2NET，请确认该目录是官方 U-2-Net 仓库。"
        ) from exc

    if not weights_path.exists():
        raise FileNotFoundError(f"找不到预训练权重：{weights_path}")

    model = U2NET(3, 1)

    # 新版 PyTorch 支持 weights_only=True；旧版则回退到普通加载。
    try:
        state_dict = torch.load(
            weights_path,
            map_location=device,
            weights_only=True,
        )
    except TypeError:
        state_dict = torch.load(weights_path, map_location=device)

    model.load_state_dict(state_dict)
    model.to(device)
    model.eval()
    return model


def preprocess(image: Image.Image) -> torch.Tensor:
    resized = image.resize((320, 320), Image.Resampling.BILINEAR)
    array = np.asarray(resized, dtype=np.float32) / 255.0

    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    array = (array - mean) / std

    chw = np.transpose(array, (2, 0, 1))
    return torch.from_numpy(chw).unsqueeze(0).float()


def normalize_prediction(prediction: torch.Tensor) -> torch.Tensor:
    minimum = torch.min(prediction)
    maximum = torch.max(prediction)
    denominator = maximum - minimum

    if float(denominator) < 1e-8:
        return torch.zeros_like(prediction)

    return (prediction - minimum) / denominator


def infer(
    model: torch.nn.Module,
    input_tensor: torch.Tensor,
    device: torch.device,
) -> np.ndarray:
    with torch.inference_mode():
        outputs = model(input_tensor.to(device))
        primary_output = outputs[0][:, 0, :, :]
        normalized = normalize_prediction(primary_output)

    return normalized.squeeze(0).detach().cpu().numpy()


def resize_probability_map(probability: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    probability_u8 = np.clip(probability * 255.0, 0, 255).astype(np.uint8)
    saliency_image = Image.fromarray(probability_u8, mode="L")
    saliency_image = saliency_image.resize(size, Image.Resampling.BILINEAR)
    return np.asarray(saliency_image, dtype=np.uint8)


def compute_bbox(mask: np.ndarray) -> list[int] | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None

    # [x_min, y_min, x_max, y_max]，其中最大坐标为闭区间。
    return [
        int(xs.min()),
        int(ys.min()),
        int(xs.max()),
        int(ys.max()),
    ]


def create_overlay(image: Image.Image, mask_u8: np.ndarray) -> Image.Image:
    original = np.asarray(image, dtype=np.float32)
    alpha = (mask_u8.astype(np.float32) / 255.0)[..., None] * 0.45

    marker = np.zeros_like(original)
    marker[..., 0] = 255.0

    overlay = original * (1.0 - alpha) + marker * alpha
    return Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8), mode="RGB")


def save_results(
    image_path: Path,
    image: Image.Image,
    saliency_u8: np.ndarray,
    threshold: float,
    output_dir: Path,
    device: torch.device,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = image_path.stem

    threshold_value = int(round(threshold * 255.0))
    mask = saliency_u8 >= threshold_value
    mask_u8 = (mask.astype(np.uint8) * 255)

    saliency_path = output_dir / f"{stem}_saliency.png"
    mask_path = output_dir / f"{stem}_mask.png"
    overlay_path = output_dir / f"{stem}_overlay.png"
    metadata_path = output_dir / f"{stem}_region.json"

    Image.fromarray(saliency_u8, mode="L").save(saliency_path)
    Image.fromarray(mask_u8, mode="L").save(mask_path)
    create_overlay(image, mask_u8).save(overlay_path)

    width, height = image.size
    important_pixels = int(mask.sum())
    total_pixels = int(mask.size)

    metadata: dict[str, Any] = {
        "source_image": str(image_path.resolve()),
        "width": width,
        "height": height,
        "coordinate_system": {
            "origin": "top-left",
            "x_direction": "right",
            "y_direction": "down",
        },
        "threshold": threshold,
        "important_pixel_count": important_pixels,
        "total_pixel_count": total_pixels,
        "important_area_ratio": important_pixels / total_pixels,
        "bounding_box_xyxy": compute_bbox(mask),
        "device": str(device),
        "files": {
            "saliency": str(saliency_path.resolve()),
            "mask": str(mask_path.resolve()),
            "overlay": str(overlay_path.resolve()),
        },
    }

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    args = parse_args()

    if not 0.0 <= args.threshold <= 1.0:
        raise ValueError("--threshold 必须位于 0 到 1 之间。")

    image_path = args.image.resolve()
    if not image_path.exists():
        raise FileNotFoundError(f"找不到输入图片：{image_path}")

    repo_dir = args.repo.resolve()
    weights_path = (
        args.weights.resolve()
        if args.weights is not None
        else repo_dir / "saved_models" / "u2net" / "u2net.pth"
    )

    device = select_device(args.device)
    print(f"[1/4] 运行设备：{device}")

    print("[2/4] 加载 U²-Net 模型……")
    model = load_model(repo_dir, weights_path, device)

    print(f"[3/4] 处理图片：{image_path.name}")
    image = Image.open(image_path).convert("RGB")
    input_tensor = preprocess(image)
    probability_320 = infer(model, input_tensor, device)
    saliency_u8 = resize_probability_map(probability_320, image.size)

    print("[4/4] 保存结果……")
    metadata = save_results(
        image_path=image_path,
        image=image,
        saliency_u8=saliency_u8,
        threshold=args.threshold,
        output_dir=args.output.resolve(),
        device=device,
    )

    print("完成。")
    print(f"重要区域占比：{metadata['important_area_ratio']:.4%}")
    print(f"外接矩形 xyxy：{metadata['bounding_box_xyxy']}")
    print(f"输出目录：{args.output.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc

