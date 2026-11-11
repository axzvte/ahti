# 固定重要区域

当前流程为 U²-Net 与 RC 联合统计。

1. 用预训练 U²-Net 得到原尺寸显著图，按字节阈值 128 生成二值掩膜。
2. 用 Felzenszwalb 分割生成区域，根据 Lab 颜色直方图差异、区域大小、质心距离计算 RC 分数。
3. 每张图的 RC 响应用 Otsu 二值化，严格大于阈值为前景，常量响应生成空掩膜。
4. 逐图取 U²-Net 与 RC 的并集，按像素中心直接拉伸采样到 320×320。
5. 统计同一坐标被多少张图选中，取频率至少 85% 的点，求最小外接矩形。

850 张图的 85% 阈值对应至少 723 张。高频点的外接框可以包含较低频位置。

`union_frequency_rectangles.py` 输出协议、逐图统计、掩膜、频率图、高频掩膜、矩形与预览。`color_saliency.py` 只保留 RC。`mask_utils.py` 提供采样、Otsu 与导出函数。`infer_u2net.py` 和 `batch_infer_u2net.py` 负责预训练模型推理。

第三方模型结构和许可证在 `third_party/U-2-Net/`，权重通过 `--weights` 指定。运行命令见根目录 README。
