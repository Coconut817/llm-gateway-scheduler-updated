# 接手同学使用说明

更新日期：2026-10-07。先按本文跑通，再读 [改动汇总](handoff-guide.md)、[动态功能细节](dynamic-output.md) 与 [实验档案](change-history.md)。

## 入口和运行模型

baseline.py是正式单次运行入口；config/*.json定义条件；examples/中的脚本组织多次对照或诊断，不是运行功能的必需入口。

模型不实际执行，endpoint是模拟的，input/output已知，时间为虚拟毫秒。一行完整prompt/response对应一次请求，历史计input、外层response计output。

## 1. 环境

进入包含baseline.py的项目目录（换机器用自己的路径）：

```powershell
cd D:\Phd\1\endpoint\scheduler\llm-gateway-scheduler
& .\.venv\Scripts\python.exe --version
```

已有.venv可直接使用。新机器用已安装的Python 3.12创建：

```powershell
python --version
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

python/py不存在或指向Windows商店占位程序时，用已安装解释器的完整路径。不要求安装py启动器；.venv一般应在新机器重建。

## 2. 先跑9条样例

无需原始数据或tokenizer，仍需安装Python依赖：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_all_requests.json `
  --source examples/length_requests.jsonl --source-format lengths `
  --output-dir workload_profiling/results/reproduction/handoff_smoke
```

预期9条完成、拒绝0、endpoint_executed_requests=9。这里是all模式，轻型也消耗端点；旧examples.run_baseline是heavy_only，不能混淆。

## 3. 当前动态功能

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_dynamic_output.json `
  --output-dir workload_profiling/results/reproduction/handoff_dynamic
```

不设--limit即读取全量原始3168行。首次下载固定Qwen tokenizer/config/template，不下载权重、不需要PyTorch；缓存完整后可离线计数。超出模型上下文警告不等于回放失败，本项目仅计数不执行模型。

除了原始数据，百分位功能需要配套资源：

```text
workload_profiling/data/artifacts/output_percentile_reference.parquet
workload_profiling/results/stage2_1/percentile_reference_metadata.json
workload_profiling/config/dynamic_output_policy.json
```

前两项哈希与样本数须匹配。缺失时在交接包补齐或用导出副本，不用为了试运行先重跑Stage 2。

预设使用全请求执行、突发、额度充足、并发8/12/16、同速无波动、min_rpm；输入固定、输出动态；轻先重后同类FIFO、批间FIFO。当前没有SLO统计或主引擎跨批选择。

## 4. 固定80%对照

只改分类模式，其他条件一致：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_dynamic_output.json `
  --output-classification percentile_fixed --output-percentile-threshold 0.8 `
  --output-dir workload_profiling/results/reproduction/handoff_fixed80
```

比较全部请求延迟和固定请求集合；动态heavy成员变化，不能只比较各自重型均值。既有三组报告在results/reproduction/dynamic_output_v1/comparison.md。

## 5. 参数和输入

| 需求 | 参数/位置 |
| --- | --- |
| 全请求执行 | batch_scope=all；百分位模式必须all |
| 批大小、最大收集等待 | --batch-size、--batch-wait-ms，任一条件满足即释放 |
| 固定/突发到达 | arrival_mode、burst_size、burst_span_ms；不是实际派发速率 |
| 输入固定阈值 | --input-threshold；当前未自动接EVT |
| 输出模式与门槛 | --output-classification、--output-percentile-threshold |
| 压力规则与门槛映射 | 复制dynamic_output_policy.json，再用--output-policy指向副本 |
| endpoint速度/容量/波动 | 复制场景JSON修改endpoints |
| 排序与路由 | --batch-order与--strategy；具体契约见extensions.md |

百分位每批释放前更新，不是每20ms固定更新；20ms是收集上限。已分类的等待请求不重新分类。

长度JSONL每行一个对象，tokens非负整数，ID唯一：

```json
{"request_id":"r1","input_tokens":1024,"output_tokens":256}
```

缺失长度不能默默填0。当前到达时刻由fixed/burst生成，输入JSONL中的arrival_at_ms不会自动生效；历史网关数据也不能直接按prompt格式读入。接入新公司日志前先适配字段，流式标志目前不改变调度逻辑。

优先级是可选接口，数值大优先，缺省1/normal/default。uniform不覆盖已有字段，平级实验应使用无标签输入或显式统一。若业务权重有需要，明确提供base_priority与priority_class并选择effective_priority排序；当前light_first_fifo不使用不同业务权重。仅填权重时类别仍按历史10/3/1推断，不要把1/2/3当作“数字小优先”编号。

## 6. 看结果

| 文件 | 重点 |
| --- | --- |
| summary.json/report.md | 完成/拒绝数、真实执行数、全部延迟、实际模式和门槛 |
| requests.csv | 每条分类、端点、服务时间、收集/容量等待 |
| threshold_trace.csv | 每批压力、前后门槛、长度界限和分类数 |
| events.jsonl | arrived、classification_updated、batch_released、dispatched、completed、capacity_wait |
| batches.json/endpoints.json | 计划顺序、端点负载和峰值 |
| 策略/参考副本及replay_config.json | 复现当次规则 |

capacity_wait_ms包含队首阻挡，只发生在批释放后。请求完成仅释放并发；RPM/TPM记录需60秒滚动窗口过期才恢复。额度充足预设在当前数据中排除了分钟额度等待，并发仍有限。

普通CLI会覆盖同名文件，不清理其他旧文件，每组用新目录。切换tokens模式后残留的threshold_trace不能当本次结果。对照脚本一般拒绝已有输出根目录。

## 7. 导出副本复现

当前已有完整资源和长度输入时可执行：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/results/reproduction/dynamic_feature_v2/main/replay_config.json `
  --source workload_profiling/results/reproduction/dynamic_feature_v2/full_requests.jsonl `
  --source-format lengths `
  --output-dir workload_profiling/results/reproduction/handoff_replay
```

长度模式无需tokenizer。策略/参考的相对路径按replay_config目录解析，输入要另外携带。纯代码包缺少结果目录时，先运行步骤3导出自己的副本。资源自定义方法见dynamic-output.md。

## 8. 最小验证

```powershell
& .\.venv\Scripts\python.exe -m unittest `
  workload_profiling.tests.test_baseline `
  workload_profiling.tests.test_baseline_ordering `
  workload_profiling.tests.test_service_jitter `
  workload_profiling.tests.test_burst_arrivals `
  workload_profiling.tests.test_all_requests `
  workload_profiling.tests.test_priority `
  workload_profiling.tests.test_concurrency_audit `
  workload_profiling.tests.test_dynamic_classification `
  workload_profiling.tests.test_dynamic_entry `
  workload_profiling.tests.test_runtime_policy `
  workload_profiling.tests.test_io
```

交付时90项通过，验证行为和复现，不证明性能更好。全部科学实验的测试条件另见running.md、offline-profiling.md。

## 交接包

- 必带源码、requirements.txt、config、docs、小样例，不只复制examples。
- 原文模式带prompt数据或指定新的--source；百分位带参考Parquet与metadata，或整套导出资源。
- 历史对照需附正式结果和脚本依赖的基础数据。部分历史脚本读取provenance.source绝对路径，换机器应先建立本机基线或指定reference，不照抄旧机器路径。
- .venv、tokenizer缓存、临时cache可以重建。结果不一定随Git/ZIP自动带上，交接前确认。

下一步尚需明确SLO、输入EVT接入与更新、跨批是否进入主代码，并用独立数据验证动态规则。这些不属于已完成交付。
