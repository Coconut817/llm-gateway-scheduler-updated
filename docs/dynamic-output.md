# 动态输出重型门槛：使用说明

交付日期：2026-10-07。本功能已接入普通 baseline.py，不需要通过对照实验脚本才能启用。

## 一条命令运行

在项目根目录执行，输出目录请取新名称：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_dynamic_output.json `
  --output-dir workload_profiling/results/reproduction/my_dynamic_run
```

默认读取原始3168条完整prompt/response，使用已缓存的固定Qwen tokenizer计数。也可指定--source与--source-format lengths使用已知长度JSONL。所有请求均入窗并模拟执行，默认基础优先级1；不发送真实模型请求。

该预设使用突发到达、16条批、20ms收集上限、额度充足、并发8/12/16、同速端点、波动关闭和min_rpm。这里更新的是输出重型判定门槛，输入阈值仍固定40342.5；没有接入输入在线EVT、SLO、跨批主排序或真实网关。

## 判定与调度

每批释放前，读取当时并发、已经释放的ready队列及本批已到达数量，确定本批门槛。输出tokens在冻结参考ECDF中的百分位>=门槛，判为输出重型；与输入重型取OR。整批共用门槛，等待期间不重新分类。

排序为light_first_fifo：轻型先、重型后，同类按到达顺序；这是平级实验规则，不使用不同业务等级排序。其他可选排序仍可通过--batch-order指定；纯输出最短排序只记录分类，不使用标签决策。

U=在途请求数/总并发上限；E=max(0,ready请求数+本批请求数-空闲名额)。E只使用已经到达的需求，不前视未来请求。

| 匹配顺序 | 压力条件 | 门槛默认值 |
| --- | --- | --- |
| 1 | E>=总名额×critical_excess_ratio | 60% |
| 2 | E>0或U>=busy_concurrency_threshold | 70% |
| 3 | U>=normal_concurrency_threshold | 80% |
| 4 | 其他 | 90% |

内部档位idle/normal/busy/critical只是压力标签，不改变“当前研究繁忙分支”的假设。门槛按批更新，不是墙钟后台任务。本版没有平滑/滞回，也不保证动态门槛降低延迟。

## 修改门槛和压力规则

规则文件workload_profiling/config/dynamic_output_policy.json同时保存门槛映射与pressure_rules。初始值分别为0.6/0.85/1.0，影响匹配上表的压力区间。

建议复制成自己的JSON再运行：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_dynamic_output.json `
  --output-policy workload_profiling/config/my_output_policy.json `
  --output-dir workload_profiling/results/reproduction/my_policy_run
```

规则验证：所有百分位在(0,1)内，压力越高门槛不能越高；0<=normal_concurrency_threshold<busy_concurrency_threshold<=1，critical_excess_ratio>0。未知pressure_rules字段、非法值在处理请求前报错。

--output-percentile-threshold设置固定模式门槛或动态模式的初始状态，允许(0,1)内的值。动态模式首次采样立即按压力映射更新，因此初始值未必用于第一批分类。

固定80%对照：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_dynamic_output.json `
  --output-classification percentile_fixed `
  --output-percentile-threshold 0.8 `
  --output-dir workload_profiling/results/reproduction/my_fixed80_run
```

原tokens模式仍是默认，旧配置和旧轻型处理语义保持兼容。百分位模式必须batch_scope=all。

## 更换参考分布

默认使用data/artifacts/output_percentile_reference.parquet与results/stage2_1/percentile_reference_metadata.json，加载时校验文件哈希、样本数和ECDF内容。默认参考来自5186个历史展开样本，与3168完整行口径不同且来自相关语料，不能当作独立测试验证。

可用--output-reference与--output-reference-metadata同时指定新的Parquet和metadata。也可在配置中设置output_reference_path、output_reference_metadata_path；必须成对提供。output_policy_path也可在配置中设置。配置里的相对资源路径相对配置文件所在目录解析，CLI传入的路径相对命令调用目录解析。新字段缺省时仍使用包内默认资源。

## 每次运行自动保存

除原config.json、summary.json、requests.csv、events.jsonl、batches.json、endpoints.json、report.md外，百分位模式自动输出：

| 文件 | 用途 |
| --- | --- |
| threshold_trace.csv | 每批压力、前后门槛、长度界限与轻重数量；空输入也保存表头 |
| classification_policy.json | 实际使用的门槛映射与压力规则快照 |
| classification_metadata.json | 模式、初始值、来源和导出文件哈希 |
| output_percentile_reference.parquet | 当次冻结参考的独立副本 |
| output_reference_metadata.json | 副本样本数、ECDF定义及哈希 |
| replay_config.json | 指向同目录策略和参考副本的复现配置 |

requests.csv含每请求output_percentile、output_percentile_threshold、output_token_cutoff、pressure_level、classified_at_ms等字段；events.jsonl含classification_updated。summary.json记录实际门槛集合、变化次数、参考样本数和分类provenance。

结果资源快照可以用于重放，仍需提供相同请求输入：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/results/reproduction/my_dynamic_run/replay_config.json `
  --source my_requests.jsonl --source-format lengths `
  --output-dir workload_profiling/results/reproduction/my_replay_run
```

relative资源路径使策略/参考副本随目录移动可继续加载；源请求与Python依赖并未复制进每个结果目录。普通CLI重复指定已有目录会替换标准产物，建议新建输出目录。

## 已完成验证

实际完整原文CLI结果在results/reproduction/dynamic_feature_v2/main。3168条全部执行，无拒绝，20次门槛变化，实际使用60/70/90%；平均134.874053ms、P95=296ms。数值与此前dynamic_output_v1一致，未因此声称性能提升。

同目录full_requests.jsonl是从本次实际计数结果导出的无业务标签长度输入，保留全部3168条；replay/用main/replay_config.json及此输入运行，请求、事件、批、端点计数和门槛轨迹逐值完全一致；fixed_80/在相同输入与资源快照下验证固定80%且变化次数为0。验收和哈希在acceptance_checks.json。

90项相关测试通过，新增test_dynamic_entry检查自定义规则生效、非法规则、快照隔离、原始策略文件移除后依靠导出副本复现、普通execute入口、固定模式、空输入与资源路径。旧token模式相关测试和旧runtime百分位策略测试均通过。

结论：可配置动态功能与复现入口已完成；当前批内规则的性能收益仍接近0，需要继续研究策略，不把功能验证等同于性能优势。
