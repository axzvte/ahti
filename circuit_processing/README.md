# 电路分析与固定配置

当前四个应用是 7×7 高斯、7×7 Sobel、复用 8×8 DCT 与复用 9/7 DWT。它们使用同一个 C 编码器、误差传播器和基准优先选择器。

C 内核通过 AST 编码成有类型的标量 SSA 图。`id` 对应展开运算实例，`unit_node` 对应可复用的加法器组。图片用于后续验证。

候选覆盖不同传播增益，按带符号的名义输出响应估计块内均方扰动。先检查零替换候选，用完整块误差上界保证全图 PSNR 下界至少 30 dB。区间、线性尾部能量和相关翻转界取可用的较紧值。输出响应与有效影响像素数用于排序，实际区域 PSNR 由图片验证。

零替换未在预算内认证时，按误差降低和单元面积代价逐步尝试低误差近似单元或精确单元，只接受降低全图误差上界的替换。候选与分析次数均有上限，不枚举全部组合，也不声明全局最优。未认证和实测超标分别记录。

操作数限制为有符号 12 位，近似加法器输出为有符号 13 位。第 0 至 11 位可用 XOR 翻转。完整块落入 ROI 时才开启翻转，补偿单元在全图生效。

| 文件 | 职责 |
|---|---|
| `configs/circuits/*.json` | 当前四个 C 接口与原基准 |
| `kernels/*.c` | 四个算术内核 |
| `scripts/c_kernel_encoder.py` | AST 展开与节点连接 |
| `scripts/circuit_sources.py` | 加载 C 接口或 IR |
| `scripts/circuit_ir.py` | 结构校验、范围、误差传播 |
| `scripts/circuit_energy.py` | 能量证书和带符号响应 |
| `scripts/unit_policy.py` | 基准优先、按需补偿 |
| `scripts/fixed_roi_pipeline.py` | 缩放、冻结、验证、Excel |
| `app/circuit_ir.hpp` | 通用 C++ 执行器 |
| `hardware/scripts/generate_rtl.py` | 固定尺寸 RTL |
| `hardware/scripts/run_dc_core_area.sh` | 串行 DC |
| `hardware/scripts/summarize_area.py` | 面积汇总 |

`configs/unit_error_bounds.json` 保存全输入审计界和源文件指纹。`arithmetic_sources.json` 保护当前算术源码。`nangate45_unit_costs.json` 用于配置选择的面积估计，最终面积由 DC 确定。

整理后从头生成 schema 5 冻结文件，其中包含输入图、ROI、选点、替换、证书、候选轨迹和源码指纹。修改源码后需重新冻结。C 编码接口和命令可用：

```bash
python circuit_processing/scripts/fixed_roi_pipeline.py --help
python circuit_processing/scripts/fixed_roi_pipeline.py encode-c --help
```

原/新两组由 `freeze --baseline original` 和 `freeze --baseline changed --reference 原配置` 生成。`--circuit` 可重复指定 C 描述或 IR。软件使用通用执行器，RTL 当前映射随附的四个内核。

高斯与 Sobel 由外部供给窗口。DCT 复用一个一维前向核心，含行缓存、系数寄存器和 16 周期调度，逆变换用于软件验证。DWT 固定点提升核心包含正逆变换、块缓存和 1,152 周期调度。完整运行步骤见根目录 README。
