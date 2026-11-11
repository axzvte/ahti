"""Helpers for U2-Net + RC masks, stretch coordinates and frequency output."""
from __future__ import annotations
import csv
import json
import numpy as np
from PIL import Image, ImageDraw

def otsu_threshold(values: np.ndarray, bins: int = 256) -> float | None:
    """Return a data-derived Otsu threshold; None means no contrast in values."""
    data = np.asarray(values, dtype=np.float64)
    data = data[np.isfinite(data)]
    if data.size == 0:
        return None
    low, high = float(data.min()), float(data.max())
    if high - low <= 1e-12:
        return None
    hist, edges = np.histogram(data, bins=bins, range=(low, high))
    probability = hist.astype(np.float64) / float(hist.sum())
    centers = (edges[:-1] + edges[1:]) * 0.5
    weight_left = np.cumsum(probability)
    mean_left = np.cumsum(probability * centers)
    mean_total = float(mean_left[-1])
    denominator = weight_left * (1.0 - weight_left)
    between = np.zeros_like(denominator)
    valid = denominator > 1e-15
    between[valid] = (
        mean_total * weight_left[valid] - mean_left[valid]
    ) ** 2 / denominator[valid]
    if not np.any(valid):
        return None
    index = int(np.argmax(between))
    return float((edges[index] + edges[index + 1]) * 0.5)


def otsu_mask(values: np.ndarray) -> tuple[np.ndarray, float | None]:
    """High-response candidate pixels; a constant map yields an empty mask."""
    threshold = otsu_threshold(values)
    if threshold is None:
        return np.zeros(np.asarray(values).shape, dtype=bool), None
    return np.asarray(values, dtype=np.float64) > threshold, threshold


def read_mask(path):
    with Image.open(path) as image:
        if image.format != 'PNG' or image.mode not in ('1', 'L'):
            raise ValueError(f'Expected a single-channel binary PNG: {path}')
        array = np.array(image)
    values = set(np.unique(array).tolist())
    if not (values <= {0, 1} or values <= {0, 255}):
        raise ValueError(f'Expected values 0/1 or 0/255, found {sorted(values)}: {path}')
    return array > 0


def sample_mask(mask, grid, mode):
    """Sample continuous source pixels at canvas pixel centers; no smoothing."""
    if grid < 1 or mode != 'stretch':
        raise ValueError('Invalid grid or coordinate mode')
    height, width = mask.shape
    sx, sy = grid / width, grid / height
    dx, dy = (grid - width * sx) / 2, (grid - height * sy) / 2
    x = (np.arange(grid) + .5 - dx) / sx
    y = (np.arange(grid) + .5 - dy) / sy
    valid = ((y >= 0) & (y < height))[:, None] & ((x >= 0) & (x < width))[None, :]
    xi = np.clip(np.floor(x).astype(int), 0, width - 1)
    yi = np.clip(np.floor(y).astype(int), 0, height - 1)
    sampled = mask[np.ix_(yi, xi)] & valid
    return sampled, valid


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def heatmap(values, title, size=420):
    """Fixed 0..1 sequential scale for every frequency and difference panel."""
    scaled = np.clip(np.nan_to_num(values, nan=0), 0, 1)
    low, high = np.array([247, 251, 255]), np.array([8, 48, 107])
    rgb = np.round(low + scaled[..., None] * (high - low)).astype(np.uint8)
    rgb[~np.isfinite(values)] = [175, 175, 175]
    panel = Image.new('RGB', (size + 60, size + 110), 'white')
    draw = ImageDraw.Draw(panel)
    draw.text((20, 12), title, fill='black')
    panel.paste(Image.fromarray(rgb).resize((size, size), Image.Resampling.NEAREST), (30, 42))
    draw.rectangle((30, 42, size + 29, size + 41), outline='black')
    draw.text((30, size + 48), 'Top-left origin; x right, y down', fill='black')
    draw.text((30, size + 65), 'Gray = no valid image samples', fill='black')
    for x in range(size):
        color = tuple(np.round(low + x / (size - 1) * (high - low)).astype(int))
        draw.line((30 + x, size + 85, 30 + x, size + 94), fill=color)
    draw.text((30, size + 96), '0', fill='black')
    draw.text((size + 15, size + 96), '1', fill='black')
    return panel


def overlay(base: Image.Image, response: np.ndarray, color: tuple[int, int, int],
            opacity: float = 0.65) -> Image.Image:
    rgb = np.asarray(base.convert("RGB"), dtype=np.float32).copy()
    weight = np.clip(response, 0, 1).astype(np.float32)[..., None] * opacity
    output = rgb * (1 - weight) + np.asarray(color, dtype=np.float32) * weight
    return Image.fromarray(np.uint8(np.clip(np.rint(output), 0, 255)))


def mask_overlay(base: Image.Image, mask: np.ndarray,
                 color: tuple[int, int, int]) -> Image.Image:
    return overlay(base, mask.astype(np.float32), color, 0.55)
