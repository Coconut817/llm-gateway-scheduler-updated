# 可运行示例

所有命令从仓库根目录执行；先按 [安装说明](../docs/running.md#环境安装) 安装依赖。Windows 命令中的解释器可替换为 Linux/macOS 的 `.venv/bin/python`。

接手当前全请求/动态功能请先读 [接手说明](../docs/handoff-start.md)，通过baseline.py运行。下面1～5是原项目基础示例，默认heavy_only；后面的对照脚本索引属于新增研究工具。

| 文件 | 用途 | 是否需要 tokenizer |
| --- | --- | --- |
| `length_requests.jsonl` | 9 条人工长度请求，覆盖轻型、输入重型、输出重型和双重型 | 否 |
| `prompt_requests.jsonl` | 2 条人工 prompt/response，展示完整历史作为一次输入 | 是 |
| `current_requests.jsonl` | 2 条当前请求，供单条运行时接口读取，未附 response | 是 |
| `run_baseline.py` | 调用 `execute()`，运行长度示例并导出结果 | 否 |
| `min_tpm.py` | 可替换策略，比较候选端点 TPM 利用率 | 否 |
| `output_first.py` | 可替换批内排序，按录制输出长度降序排列请求 | 否 |
| `sender.py` | 同步 sender 契约示例，返回演示回复，不调用模型 | 是，计数接口需要 |

## 1. 先运行不依赖 tokenizer 的示例

```powershell
& .\.venv\Scripts\python.exe -m examples.run_baseline
```

预期总请求 9，轻型 5、重型 4，全部完成。批大小 2、等待 3ms，触发原因包括 `batch_size`、`timeout`、`end_of_input`。结果在 `workload_profiling/results/baseline_example/`。

等价 CLI：

```powershell
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --batch-size 2 --batch-wait-ms 3 --output-dir workload_profiling/results/baseline_example
```

## 2. 使用完整上下文的 prompt 示例

```powershell
& .\.venv\Scripts\python.exe baseline.py --source examples/prompt_requests.jsonl --batch-size 2 --batch-wait-ms 3 --output-dir workload_profiling/results/baseline_example
```

每行产生一次请求；第二行的历史 assistant 属于 input，外层 `response` 属于 output。短文本可能全部被默认阈值判为轻型；可添加 `--input-threshold 1` 强制进入重型路径，检查调度过程。

## 3. 更换策略

```powershell
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --strategy examples.min_tpm:MinTpmStrategy --batch-size 2 --batch-wait-ms 3 --output-dir workload_profiling/results/baseline_custom
```

此示例修改端点评分指标，批内仍按 FIFO；候选端点必须满足 RPM、TPM、并发限制。接入契约见 [扩展说明](../docs/extensions.md)。

## 4. 同时替换批内排序与路由

```powershell
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --batch-size 2 --batch-wait-ms 3 --batch-order examples.output_first:OutputLongestFirstOrder --strategy examples.min_tpm:MinTpmStrategy --output-dir workload_profiling/results/baseline_custom/output_first
```

仍为 9 条请求，轻型 5、重型 4，全部完成。第一批 request_ids 为 `["input_heavy_1", "output_heavy_2"]`，dispatch_order 为 `["output_heavy_2", "input_heavy_1"]`。排序改变实际派发顺序，CSV 仍保留到达顺序，batch_position 分别为 1、0。

内置最短优先用 `--batch-order shortest_first`，最长优先用 longest_first，默认 fifo。内置长短按 input+output 总 tokens 计算，自定义示例按 output_tokens 计算；它们是不同排序规则。详见 [两个接口的接入方法](../docs/extensions.md#排序与路由是两个独立接口)。

## 5. 单条接口接入 sender

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling.runtime.run_stream --input examples/current_requests.jsonl --lengths-only --sender examples.sender:send_one
```

每行输出一个长度结果。`--lengths-only` 表示不加载 Stage 2.1 策略；演示 sender 返回固定说明文本。换成自己的同步模型客户端后才会发送真实请求，详见 [Python 接口](../docs/api.md#接入已有模型客户端)。

重复使用相同输出目录会覆盖本次导出的七个标准文件；比较不同实验时请使用不同 `--output-dir`。

## 新增对照与诊断脚本

这些脚本组织实验，不是新的独立服务。对照脚本通常拒绝已有输出根目录，重跑用 `--output-dir` 取新名称。默认输入依赖如下；具体命令及完整结果见 [改动索引](../docs/handoff-guide.md) 和 [详细档案](../docs/change-history.md)。

| 模块（python -m 后的名称） | 用途 | 默认依赖 | 正式结果目录名 |
| --- | --- | --- | --- |
| examples.compare_service_jitter | 无波动/±20%对照 | handoff_v1配置与原始prompt | service_jitter_v1 |
| examples.compare_endpoint_speed | 20/20/20与30/20/10 | handoff_v1、原文 | endpoint_speed_v1 |
| examples.compare_burst_arrivals | 固定/突发到达 | handoff_v1、原文 | burst_arrivals_v1 |
| examples.rebuild_all_requests_baseline | 旧模式与全请求 | handoff_v1、原文 | all_requests_v1 |
| examples.compare_priority | 原排序/优先级/折扣，两种额度 | all_requests_v1/all_requests、原文 | priority_v1 |
| examples.compare_priority（权重参数） | 3/2/1与10/3/1 | 同上；--compare-results指定priority_v1 | priority_weights_321_v1 |
| examples.compare_priority_burst | 并发竞争下有无折扣 | priority_weights_321_v1、原文 | priority_burst_321_v1 |
| examples.diagnose_priority_burst | 独立跨批/服务时间诊断 | priority_burst_321_v1已验证CSV与批记录 | priority_burst_diagnosis_v3 |
| examples.compare_batch_sizes | 16/64/128/256/512 | priority_burst_321_v1与diagnosis_v3 | batch_size_v2 |
| examples.compare_dynamic_threshold | 平级最短输出/固定80%/动态 | priority_burst_321_v1长度CSV、冻结ECDF及metadata | dynamic_output_v1 |

结果名均在workload_profiling/results/reproduction下。部分原文对照读取reference中provenance.source的绝对路径；换机器应重建本机基础结果或正确指定reference，不能只复制旧结果后照抄命令。diagnose脚本不改主engine，也不调用模型。

辅助模块：output_shortest_first提供仅输出长度升序；concurrency_audit逐事件核对并发与额度；comparison_common允许新增优先级列并核对历史字段。它们不是需要单独启动的实验服务。

例如重跑平级动态对照（须已有默认reference资源）：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_dynamic_threshold `
  --output-dir workload_profiling/results/reproduction/handoff_dynamic_comparison
```

没有历史结果包时，先使用handoff-start.md里的普通CLI和长度样例，不必运行这串依赖实验。
