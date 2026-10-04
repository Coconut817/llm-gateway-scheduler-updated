# 历史离线长度分析与复现

[返回首页](../README.md) · [新 baseline 架构](architecture.md)

此文档说明保留的 Stage 1 / 2 / 2.1。它们不是新 baseline 的必要前置步骤。新 baseline 每原始行只读一次；Stage 1 会展开历史 assistant，因此两者请求数量与阈值解释不同。

## 依赖顺序

```text
原始 prompt/response
  -> Stage 1: request_lengths.parquet (唯一长度基表)
  -> Stage 2: tail_labels.parquet (P95 / EVT 标签)
  -> Stage 2.1: output_labels.parquet + 冻结 output ECDF
```

后续阶段只保存新增标签，使用 `(conversation_id, request_index)` 与基表关联；缺行、重复键、错配、来源哈希或标签校验和不一致报错，不按行位置拼接。

## 完整复现

先按 [运行说明](running.md#环境安装) 安装依赖，再从仓库根目录依次执行：

```powershell
# 检查原始 schema
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage1.inspect_data

# 少量真实行与展开边界检查，缓存产物
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage1.run --smoke

# 完整长度基表
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage1.run

# 完整 POT/EVT 拟合及 bootstrap
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage2.run

# reference 与默认操作性输出标签
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage2_1.run
```

完整 Stage 2 需要大量重采样拟合，耗时显著高于 baseline 回放。只想用调度基线时无需运行它。

## Stage 1

原始每行 `prompt.messages + response` 中枚举合格 assistant，前缀保留完整历史。conversation_id 是 0-based 物理行号；request_index 是包括跳过候选的 assistant 序号，可能有间隙。默认不保存文本，通过 `common.conversation.reconstruct_request()` 回溯。

当前历史报告为 3,168 行、5,186 个成功样本、592 个跳过候选、tokenization 失败 0。连续 assistant 和空回答按旧展开规则跳过；这不是新 baseline 的读取规则。固定 Qwen 模板和 output 文本计数口径与新 baseline 共用公共能力。

| 入口 | 参数 |
| --- | --- |
| `stages.stage1.inspect_data` | `--source PATH`、`--output PATH` |
| `stages.stage1.run` | `--source PATH`、`--smoke` |

正式产物在 `data/processed/request_lengths.parquet` 与 `results/stage1/`；smoke 在 `cache/smoke/stage1/`。主要报告见 [Stage 1 报告](../workload_profiling/results/stage1/stage1_report.md)。

## Stage 2

只读取长度 Parquet，不重读原文或 tokenization。Input/Output 独立进行全局 POT/GPD 诊断与阈值选择。

| 参数 | 用途 |
| --- | --- |
| `--source PATH` | 自定义长度 Parquet |
| `--workers N` | 正整数，CPU 拟合并行数，默认依据本机 CPU |
| `--smoke` | 仍读取完整长度列，降低 GOF/外层重采样次数，写缓存 |
| `--no-resume` | 不复用 checkpoint，重新计算 |
| `--refresh-report` | 校验来源与配置后，从完成的结果重建标签和报告，不重新拟合 |

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage2.run --smoke --workers 4
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage2.run --refresh-report
```

checkpoint 在本阶段结果目录的 `.checkpoints/`；数据、算法、依赖或参数变化不会复用不同签名。参数/算法改变应完整重跑，不用 refresh-report 代替新拟合。smoke 不是正式科学结果。

当前历史结果：Input EVT 候选 u≈3835.6，但 bootstrap 敏感；Output 没有稳定阈值，output_tail_evt 为 null，不能填成 false。P95/EVT 四类 workload 在任一 EVT 缺失时保持不可用。详见 [Stage 2 报告](../workload_profiling/results/stage2/stage2_report.md)。

## Stage 2.1

从 Stage 1 全部历史 output 长度构建右连续 ECDF，冻结 reference。默认 0.95 百分位阈值命中 263/5,186；包含 ties，因此与 Stage 2 严格 `output_tokens > P95` 的 257 条不同。

入口 `python -m workload_profiling.stages.stage2_1.run` 无自定义 CLI 参数。生成：

- `data/artifacts/output_percentile_reference.parquet`。
- `data/processed/output_labels.parquet`。
- `results/stage2_1/` 中的 metadata、runtime_policy_config.json、测试验收与报告。

默认磁盘标签是 DEFAULT 快照，congestion_state 为 null；动态判断不会改写历史标签，output_tail_evt 的缺失也保留。真实 RPM/TPM 拥塞估计未由这个旧模块实现；新 baseline 的模拟容量是独立模块。详见 [Stage 2.1 报告](../workload_profiling/results/stage2_1/runtime_policy_report.md)。

## 读取完整数据视图

```python
from workload_profiling.common.datasets import load_dataset

lengths = load_dataset("stage1")      # 14 列长度基表
with_tail = load_dataset("stage2")    # 当前 18 列，含可空 EVT 标签
complete = load_dataset("stage2_1")   # 当前 23 列
```

load_dataset 验证长度、定位键、metadata 来源与标签哈希。重建基表后须按顺序重建相关标签，不能继续把旧标签用于新基表。基表只保存一次，标签文件不重复保存全部长度。

导出可用 `python demo.py --export-only`，生成 `data/exports/request_workload_stage2_1.csv`。原根目录 `request_lengths_full.csv`、`request_workload_stage2_full.csv` 已归入同一 exports 目录，它们是历史可读副本，不是正式唯一来源。

## 原文回溯

```python
from workload_profiling.common.paths import DEFAULT_SOURCE
from workload_profiling.common.conversation import reconstruct_request
from workload_profiling.stages.stage1.inspect_data import read_rows

source_row = next(row for index, row, error in read_rows(DEFAULT_SOURCE)
                  if index == 0 and error is None)
# request_index 必须取基表中的有效键，不应假设每个序号都成功。
request = reconstruct_request(source_row, conversation_id=0, request_index=0)
```

训练或评估先处理会话分组及跨行重叠。原始行不是已证明独立的业务会话，随机拆分展开样本可能把相同或关联上下文分到不同集合；全量冻结 reference 也不是自动生成的训练集基准。

## 旧逐条接口与网页

完整 runtime API、同步 sender、prepare/complete 与 CongestionProvider 见 [Python API](api.md)。旧长度网页使用 `python demo.py --length-demo --open`，需上述 Stage 2.1 产物；原始 prompt 的计数能力本身不需要这些标签。

旧模块入口已经移除，使用 `workload_profiling.stages.stage1|stage2|stage2_1`。科学结果保留原实验的时间、来源与算法 provenance；文档和目录整理不表示重新拟合。
