# 固定重要区域与近似电路处理

工程保留 `roi_selection/` 和 `circuit_processing/` 两部分。前者用 U²-Net 与 RC 联合统计固定矩形。后者对高斯、Sobel、复用 DCT、复用 9/7 DWT 统一编码，先找零替换翻转配置，再按需要补偿，生成图片、PSNR、RTL 和 DC 面积。

输入图像离线统一为 320×320。ROI 统计使用原图和原尺寸掩膜，再映射到 320×320。RTL 使用固定坐标计数器。

## 目录

```text
approx_attack_validation_clean/
├── README.md
├── run.sh
├── requirements.txt
├── roi_selection/
│   ├── batch_infer_u2net.py
│   ├── union_frequency_rectangles.py
│   ├── color_saliency.py
│   ├── mask_utils.py
│   └── third_party/U-2-Net/        模型结构和许可证
└── circuit_processing/
    ├── app/                       通用 C++ IR 执行器
    ├── configs/                   当前基准、误差审计、面积表
    ├── kernels/                   当前四个 C 内核
    ├── scripts/                   编码、分析、验证、Excel
    ├── hardware/scripts/          固定尺寸 RTL、串行 DC、面积汇总
    ├── include/
    └── third_party/evoapproxlib/   近似加法器源码
```

`results/`、`build/`、`.venv/` 和生成的 RTL 会在运行时创建。数据集、U²-Net 权重、DC 程序和工艺库作为外部输入。

## 1. 安装与编译

在 WSL Ubuntu 中进入本目录。需要 Python 3.10 或更新版本、C/C++ 编译器、CMake 和 OpenCV 开发库。

```bash
sudo apt-get update
sudo apt-get install -y python3-venv build-essential cmake libopencv-dev

python3 -m venv .venv
source .venv/bin/activate
bash run.sh setup
```

每次重开终端，先进入项目并执行 `source .venv/bin/activate`。

## 2. 重新确定重要区域

原图位于 `$HOME/datasets/PASCAL-S/datasets/imgs/pascal/1.jpg` 至 `850.jpg`。重新生成掩膜时安装推理依赖，权重变量按实际位置设置。模型结构已经随工程保留。

```bash
python -m pip install -r roi_selection/requirements-u2net.txt

export DATA_DIR="$HOME/datasets/PASCAL-S"
export U2NET_WEIGHTS="$HOME/projects/u2net_roi/U-2-Net/saved_models/u2net/u2net.pth"
export MASK_RUN="$PWD/roi_selection/results/u2net_$(date +%Y%m%d_%H%M%S)"

python roi_selection/batch_infer_u2net.py \
  --data "$DATA_DIR" --weights "$U2NET_WEIGHTS" \
  --count 850 --output "$MASK_RUN"

export ROI_RUN="$PWD/roi_selection/results/union_rc_$(date +%Y%m%d_%H%M%S)"

python roi_selection/union_frequency_rectangles.py \
  --data "$DATA_DIR" --mask-dir "$MASK_RUN/pred_masks" \
  --count 850 --grid 320 --thresholds 80 81 82 83 84 85 86 87 88 89 90 \
  --output "$ROI_RUN"
```

已有完整二值掩膜时可直接用 `--mask-dir "$DATA_DIR/pred_masks"` 作为统计输入。`ROI_RUN` 必须是新目录。电路流程读取其中 85% 联合高频点的外接矩形。

## 3. 准备两套基准和 RTL

```bash
export IMAGE_DIR="$DATA_DIR/datasets/imgs/pascal"
export IMAGE_START=1
export IMAGE_COUNT=850
export RUN_DIR="$PWD/circuit_processing/results/run_$(date +%Y%m%d_%H%M%S)"

bash run.sh prepare
```

这一步在 `RUN_DIR` 生成离线输入 `input_320/`、`original/frozen.json`、`changed/frozen.json` 及各自的 `rtl_320/`。

新基准统一选择原基准中最准确的一组近似单元，换到下一档最坏误差等级，节点编号用于打破并列。区域扰动门槛取本次原基准所选配置的估计块内均方扰动。

| 应用 | 当前新基准的改动 |
|---|---|
| 高斯 | 第 11 组 5RP → 5TE |
| Sobel | 第 3 组 5QC → 5L8 |
| DCT | 第 8 组 5QC → 5L8 |
| DWT | 第 64 组 5QC → 5L8 |

`configs/accepted_hardware.json` 是清理前已确认配置的简短核对记录。每次运行从当前 ROI 和 C 内核重新分析，生成新的冻结文件。

## 4. 验证图片和 PSNR

```bash
bash run.sh verify-original
bash run.sh verify-changed
```

每次验证创建带时间戳的新目录。

- `psnr_results.xlsx` 包含逐图 PSNR 和平均值，新基准还包含同一节点与位号的仅翻转对照。
- `images.csv` 保存逐图数据和状态。
- `summary.json` 保存达标张数、均值、最低值和失败明细。
- `comparisons/` 保存精确参考、基准、最终处理的编号拼图。
- `flip_only_comparisons/` 保存新基准实验的仅翻转对照。

PSNR 以同一算法的全精确输出为参考，均值是逐图 dB 的算术平均。`joint_pass` 同时要求全图至少 30 dB 且 ROI PSNR 下降。

中断后恢复实际运行路径和图片范围，再运行中断的一组。

```bash
source .venv/bin/activate
export RUN_DIR="$(python - <<'PYRUN'
from pathlib import Path
runs = sorted(
    run for run in Path('circuit_processing/results').glob('run_*')
    if (run/'changed/frozen.json').is_file() and (run/'input_320').is_dir()
)
if not runs:
    raise SystemExit('没有已准备好的运行目录，请先执行 prepare')
print(runs[-1].resolve())
PYRUN
)"
printf 'RUN_DIR=%s\n' "$RUN_DIR"
export IMAGE_START=1
export IMAGE_COUNT=850
bash run.sh verify-changed
```

重新验证从指定图片范围的起点开始，使用新输出目录。已完整生成 Excel 的另一组可继续使用。部分拼图不代表整组完成。

## 5. 串行 DC 综合

下面是当前机器的默认路径，可通过同名变量修改。

```bash
export DC_LIB="$HOME/techlib/nangate45/stdcells.db"
export DC_SHELL="$HOME/Synopsys/dc2016/bin/dc_shell"
export DC_COMPAT_LIB="$HOME/synopsys-compat/lib"
export DC_PERIOD=10

bash run.sh dc-original
bash run.sh dc-changed
```

各组均串行执行。零替换应用生成两版；有替换时增加仅翻转版。`Results` 指向本组 DC 目录，包含各顶层的面积、时序、约束和映射后网表，以及统一的 `area_summary.csv`。

翻转净增为仅翻转减基准。补偿净增为最终版减仅翻转。总增幅以同组基准面积为分母。

DCT 的面积包含前向变换和控制，逆变换由软件完成。DWT 包含正逆变换。窗口准备、图片加载与离线缩放在电路之外。

固定 `ROI_RUN`、图片范围、`RUN_DIR` 和 DC 环境后，`bash run.sh all` 会从离线输入准备开始完成两套配置、验证、RTL 和 DC，需要新的 `RUN_DIR`。

方法和文件职责分别见两个模块内的 README。
