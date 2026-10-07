# 历次修改与实验详细档案

2026-10-07文档整理时，由旧handoff-guide全文保存而来。以下保留各阶段当时的配置、结果、判断和待办；其中早期“尚未实现”等描述应按阶段日期理解。当前状态请读 [改动汇总](handoff-guide.md)，运行请读 [接手说明](handoff-start.md)。

本次文档维护：新增接手步骤、整合改动/功能状态/配置与结果索引、补实验脚本依赖，更新入口导航并移除README的孤立合并标记。未更改代码、配置、历史实验结果或依赖版本。详细档案继续作为后续追加记录的位置。

使用验证：按handoff-start.md样例命令运行，9条全部进入endpoint完成、拒绝0，输出handoff_smoke。此验证用于核对使用说明，不作为新算法的性能结论。

维护日期：2026-10-07（动态功能正式CLI与复现资源交付）。后续每次行为、配置、运行方式或评价口径发生变化，都在本文补充记录；以“已实现 / 已运行 / 待实现”区分状态。旧结果保留，新增实验使用独立目录。本文件是当前交接入口。

## 当前研究范围与代码状态

最新交付：动态输出门槛已支持普通baseline.py、自定义规则/参考分布、自动门槛日志与资源快照、replay_config复现入口。完整使用说明见 [动态输出门槛](dynamic-output.md)。2026-10-07已用原始3168条完整数据通过CLI验证，并用导出资源逐值复现；90项相关测试通过。SLO仍未实现，输入判定仍固定，主引擎仍批间FIFO。

最新范围调整：公司没有既定业务优先级标签，优先级保留为可选输入接口。后续实验主线统一所有请求基础优先级为1，不再围绕合成高/普通/低标签判断业务收益；调度器仍可根据长度、轻重、等待及以后明确的SLO安排顺序。平级是业务基础等级相同，不等于必须FIFO。

保留跨批排序、大批大小和重型折扣作为候选组件，不删除原实验或接口。已显式重置所有基础优先级为1，完成独立平级实验dynamic_output_v1；默认配置未改、跨批仍未接入主策略。此前10/3/1、3/2/1结果属于混合优先级，不能直接当作平级结论。不能只设置uniform就把旧合成标签当作已重置，因为接口保留已有标签。

当前研究流程图右侧繁忙分支，假定系统已 busy。已实现全请求同窗执行、可选优先级接口、重型折扣以及按压力动态更新输出百分位门槛。跨批仍为独立诊断，输入阈值未动态更新、全局busy判定未实现。业务混合优先级场景作为历史与接口验证保留。

当前新基线：batch_scope=all；所有请求共享收集队列，批大小 16、最老请求收集等待最多 20ms。释放后按输出 tokens 升序（同长保持到达顺序），逐条按可用候选最小 RPM 使用占比路由。后批不能越过前批，队首容量不足时等待。所有成功派发请求预留完整 input+output tokens、消耗 RPM 和并发，并按端点服务时间完成。它不是一次模型调用的张量 batch。

默认tokens模式轻重标签仍为 input>=40342.5 OR output>=578，兼容旧配置。新实验可选percentile_fixed/percentile_dynamic：输出按冻结历史ECDF分类，输入仍用固定P95阈值40342.5，尚未自动读取EVT。all模式所有请求均执行；light_first_fifo使用分类稳定分组，effective_priority使用分类打折，纯输出最短排序不使用标签决策。

## 主要修改记录

| 阶段 | 日期 | 变化 | 代码 / 配置 | 全量结果 |
| --- | --- | --- | --- | --- |
| 交接基线对齐 | 2026-10-05 | 新增仅按输出长度升序的排序，区别于默认 FIFO 和总 tokens shortest_first | examples/output_shortest_first.py | results/reproduction/handoff_v1（用户终端运行） |
| 耗时波动 | 2026-10-05 | EndpointConfig 新增幅度和种子，使用 request_id+endpoint_id+seed 的稳定哈希；默认关闭，波动乘整个未取整服务时间 | baseline/config.py、models.py；examples/compare_service_jitter.py | results/reproduction/service_jitter_v1 |
| 端点速度差异 | 2026-10-05 | A/B/C 输出速度 30/20/10，其他条件不变、波动关闭；路由仍不读速度 | config/baseline_endpoint_speed.json；examples/compare_endpoint_speed.py | results/reproduction/endpoint_speed_v1 |
| 突发到达 | 2026-10-05 | 新增 fixed/burst；分组集中到达，保持请求顺序和首末时刻；同毫秒先处理全部到达再调度 | baseline/arrivals.py、engine.py、config.py、run.py；config/baseline_burst.json | results/reproduction/burst_arrivals_v1 |
| 所有请求入窗执行 | 2026-10-06 | 新增 heavy_only/all 开关；all 下取消轻型零耗时完成，轻重一起入窗并消耗端点容量；增加全请求和轻型指标 | baseline/config.py、engine.py、run.py、reporting.py；config/baseline_all_requests.json | results/reproduction/all_requests_v1 |
| 固定阈值业务优先级 | 2026-10-06 | 新增基础优先级、固定种子合成标签、重型折扣、窗口内有效优先级排序与业务分组指标；两种额度场景各三组全量对照 | baseline/priority.py、models.py、ordering.py、engine.py、source.py；config/baseline_priority*.json；examples/compare_priority.py | results/reproduction/priority_v1 |
| 3/2/1权重对照 | 2026-10-06 | 业务权重可配置，保留相同业务标签，比较跨等级折扣与旧10/3/1 | baseline/config.py、priority.py；examples/compare_priority.py | results/reproduction/priority_weights_321_v1 |
| 并发竞争下折扣对照 | 2026-10-06 | 全请求+突发+额度充足，保持36个并发槽；只比较3/2/1有无0.5折扣；逐事件重建确认等待仅由并发全满造成 | config/baseline_priority_burst_321.json；examples/compare_priority_burst.py、concurrency_audit.py | results/reproduction/priority_burst_321_v1 |
| 小收益原因诊断 | 2026-10-06 | 独立重放原批释放记录，比较批内/跨批排序及输出长度/服务时间同分指标，主引擎行为未修改 | examples/diagnose_priority_burst.py | results/reproduction/priority_burst_diagnosis_v3 |
| 批大小对照 | 2026-10-06 | 同一全请求突发场景下固定20ms超时，主引擎比较批大小16/64/128/256/512，各有无折扣；补与跨批诊断的方案比较 | examples/compare_batch_sizes.py | results/reproduction/batch_size_v2 |
| 平级动态输出门槛 | 2026-10-06 | 全请求基础优先级1，冻结输出参考和输入阈值；比较最短输出、固定80%、压力动态门槛；按批释放前采样分类并记录 | baseline/classification.py、engine.py、ordering.py、config.py；config/baseline_dynamic_output.json、dynamic_output_policy.json；examples/compare_dynamic_threshold.py | results/reproduction/dynamic_output_v1 |
| 动态CLI交付与独立复现 | 2026-10-07 | 压力参数可配置、单调性与参数校验、普通CLI自动导出轨迹/策略/参考副本与replay配置；支持相对资源路径及自定义资源 | baseline/classification.py、reporting.py、run.py、config.py；runtime/output_heavy_policy.py；tests/test_dynamic_entry.py；docs/dynamic-output.md | results/reproduction/dynamic_feature_v2/main、replay、fixed_80 |

上表 baseline/、config/、results/ 均相对 workload_profiling/；examples/ 相对仓库根。耗时波动、速度差异、突发到达的旧实验均使用 heavy_only，不能当作新的全请求模型下的结果。

## 环境与入口

进入包含 baseline.py 的项目根目录：

```powershell
cd D:\Phd\1\endpoint\scheduler\llm-gateway-scheduler
& .\.venv\Scripts\python.exe --version
```

现有 .venv 使用 Python 3.12。新机器按 docs/running.md 创建环境并安装 requirements.txt。原始 prompt 模式首次下载固定 Qwen/Qwen3-8B tokenizer（不下载权重），每物理行对应一次完整请求；不展开历史回合。所有时间都是虚拟毫秒，不发送真实模型请求。

根 baseline.py 是 CLI 入口；workload_profiling/baseline/ 是引擎；config/ 是设置；examples/ 是策略和实验组织脚本；results/ 是产物。通过工具运行不会出现在用户自己打开的 PowerShell 终端，但产物在同一工作区。

## 运行新的全请求基线

单独运行新模型，不生成旧模型对照：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_all_requests.json `
  --output-dir workload_profiling/results/reproduction/all_requests_manual_v1
```

全量建立基线并核对旧模式（本次已执行）：

```powershell
& .\.venv\Scripts\python.exe -m examples.rebuild_all_requests_baseline
```

这个脚本读取 handoff_v1 的配置与原始数据、核对数据哈希和 tokenizer，只改变 batch_scope。原始文本重新计数一次，两组复用同一组请求。旧模式 requests.csv 必须与 handoff_v1 完全一致；新模式验证所有请求有批和端点、时序分解正确、tokens 守恒、容量不超限。结果根目录已存在会报错，重复运行使用 --output-dir 指定新名称。

结果目录：

```text
workload_profiling/results/reproduction/all_requests_v1/
├── comparison.md                  # 工作量模型前后变化报告，不是策略优劣比较
├── comparison.json                # 两组汇总
├── acceptance_checks.json         # 验收结果及文件哈希
├── legacy_heavy_only/              # 旧模式兼容性回放
└── all_requests/                   # 新基线：完整配置、汇总、请求和事件日志
```

all_requests/ 的 config.json 是实际配置；requests.csv 是所有请求；events.jsonl 是 arrived、batch_released、dispatched、completed、capacity_wait 等调度事件，不是原始 prompt 或实际 API 日志。batches.json 保存到达顺序和排序顺序，endpoints.json 保存累计负载与峰值，report.md 是可读报告。

普通 baseline.py 会覆盖指定目录中的标准产物；请使用新目录。对照脚本则拒绝复用已有输出根目录。

## 旧模式与兼容性

未提供 batch_scope 的旧配置仍默认为 heavy_only，根 config/baseline.json 默认配置未修改。也可以明确设置 --batch-scope heavy_only。旧模式轻型在到达时零耗时完成且不消耗端点；这只是历史实验简化，不是轻请求被立即发送到真实 endpoint。

all 模式下 batch_size 按轻重合计计数，窗口从最老的任何请求开始计时，新到达不重置计时器。全批释放、超时、EOF 三种触发仍有效。capacity_wait_ms 是批释放后至派发的等待，也包含队首阻挡；不受 20ms 收集超时限制。

## 评价口径与负载注意事项

summary 新增 batch_scope、endpoint_executed_requests、endpoint_executed_light_requests、all_latency_ms、light_latency_ms、all_queue_wait_ms、all_batch_wait_ms、all_capacity_wait_ms、capacity_wait_requests。mean/p95/max 只统计已完成请求，拒绝数另外报告；capacity_wait_requests 同样仅计 completed 且等待>0 的请求。原有 heavy_latency_ms 等字段保留。P95 使用升序后 ceil(0.95*N) 的值。

旧模式共有 3168 条逻辑请求，但端点只处理 323 条；新模式处理全部 3168 条。因此旧模式全请求延迟含大量人为的零值，不能直接用新旧均值评价算法优劣。后续固定阈值和动态阈值策略都应在 all 模式下比较相同请求，并报告全部请求和固定分组指标，避免重型集合变化导致比较失真。

当前合计 RPM 为 600+900+1200=2700/分钟，3168 条全部执行必然跨越分钟额度窗口。TPM 与并发也可能限制吞吐；几十秒的虚拟等待不代表程序实际卡住。window_ms 固定为 60000，完成只释放并发，RPM/TPM 在派发记录满一分钟后过期。若后续调整容量，另建配置记录，不能静默改变本次基线。

## 验证与后续工作

2026-10-06 全量新基线已完成：3168 条全部入窗并由端点执行，其中轻型 2845、重型 323，拒绝 0。198 个批均由达到 batch_size 触发；497 条完成请求有容量等待，最大 57312ms。全请求平均延迟 8493.78ms，P95 57329ms，虚拟模拟结束时刻 60666ms。旧模式兼容性回放与 handoff_v1 完全一致。这些数值是本次固定到达、原容量、无波动配置的结果，不能用于声称策略提升。

行为检查命令：

```powershell
& .\.venv\Scripts\python.exe -m unittest `
  workload_profiling.tests.test_baseline `
  workload_profiling.tests.test_baseline_ordering `
  workload_profiling.tests.test_service_jitter `
  workload_profiling.tests.test_burst_arrivals `
  workload_profiling.tests.test_all_requests
```

本阶段 43 项相关测试通过。覆盖旧模式默认值、轻重同批、轻请求消耗容量、并发/RPM 等待、timeout/EOF、空输入以及与突发、波动、自定义排序组合。全量验证结果以本次 acceptance_checks.json 和对应文件哈希为准。

待实现：输入EVT与调度衔接及在线更新、SLO、非繁忙endpoint池、动态规则平滑/滞回。动态输出百分位已完成第一轮；没有实现流式/非流式差异、真实API、价格/Pareto或主引擎跨批选择。已实现内容与研究效果需分别看，不能把动态机制生效当作性能改善。

## 业务优先级与重型折扣（2026-10-06）

原数据没有真实优先级。本次合成标签按 SHA-256(JSON[20261006,request_id]) 前8字节转整数后 mod100：0..19为high、权重10；20..79为normal、权重3；80..99为low、权重1。目标比例20%/60%/20%，实际人数639/1902/627。标签不读取长度，不随派发顺序变化。所有实验使用同一组标签，保存 priority_labels.csv 和哈希。

有效优先级=base_priority*(heavy时heavy_priority_discount，否则1)。数值越大越优先；effective_priority 排序按分数降序，再按输出 tokens 升序，同分同长保持到达顺序。折扣满足0<discount<=1，本次0.5。仍只做窗口内排序，后批不能越过前批，没有运行中抢占或防饥饿。控制组 output_shortest 也记录标签，但不使用标签决策。

新增请求字段 base_priority、priority_class、priority_source（default/recorded/synthetic）。JSONL 可在顶层提供 base_priority 和可选 high/normal/low/custom 类别；显式字段优先于合成标签。10/3/1自动推断high/normal/low，其他显式权重默认custom。原请求的前四个位置参数保持兼容，默认权重1；Python显式设置非默认权重/类别不会被覆盖。

新增配置 priority_assignment（默认uniform，synthetic生成标签）、priority_seed（默认20261006）、heavy_priority_discount（默认1）、batch_order=effective_priority。默认配置不自动启用新排序。新增请求日志 effective_priority、priority_discount；summary.priority_groups按基础业务类别统计数量、完成/拒绝、延迟和等待。打折不改变业务类别名称。

全量对照命令：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_priority
```

输出 results/reproduction/priority_v1：comparison.md为总表，comparison.json为分组指标，priority_labels.csv为标签，request_differences.csv为逐请求差异，ordering_changes.json为窗口顺序变化数量，acceptance_checks.json为验收。original_quota/和ample_quota/各有output_shortest、priority_no_discount、priority_discount三个子目录，保存完整配置和日志。重复运行通过--output-dir指定新目录。

原额度保持既有全请求基线；额度充足只把RPM/TPM乘100，并发、速度和容量比例不变。各场景内部三组共享请求、标签和批成员。原额度output_shortest组与all_requests_v1原有CSV字段逐行一致，新优先级字段是额外元数据。折扣组重复运行结果一致，六组全部3168条执行、无拒绝，tokens守恒、容量和排序经过核对。

单独运行有折扣场景：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_priority.json `
  --output-dir workload_profiling/results/reproduction/priority_manual_v1
```

额度充足时改用config/baseline_priority_ample_quota.json。新增CLI覆盖参数--priority-assignment、--priority-seed、--heavy-priority-discount；--batch-order支持effective_priority。网页还未添加这些新选项，请用CLI/Python运行。

本次：优先级改变198个窗口顺序，重型折扣额外改变22个窗口。原额度下high组平均延迟8456.68→8276.66ms，low组8070.18→8254.46ms，整体8493.78→8493.68ms，变化很小。额度充足下整体约28.85ms、P95为64ms，折扣没有明显额外收益。单个种子、固定到达不能支持普遍提升的结论。

重要限制：10/3/1配0.5折扣保持高>普通>低的业务等级顺序；同级输出重型往往本来就因输出更长而排后，额外变化主要来自输入重型。只按输出重型分类时，仅调整阈值可能不改排序，后续动态阈值实验必须检查顺序变化，而不能只看标签。

本阶段49项相关测试通过（前阶段43项加6项优先级行为测试）：标签确定性、显式字段、折扣与同分顺序、日志和实际派发一致、容量及跨批限制。上面的测试命令加workload_profiling.tests.test_priority可复查。由于CSV新增五个优先级字段，历史对照脚本已显式允许这五列；其余原有列仍逐值核对，不覆盖旧产物。

## 其他说明入口

### 2026-10-07 动态功能正式交付

运行入口：baseline.py --config workload_profiling/config/baseline_dynamic_output.json --output-dir 新目录。无需对照脚本。默认输入全量原文，也支持lengths。pressure_rules已从源码常量移到dynamic_output_policy.json，旧文件无此字段时仍按0.6/0.85/1.0默认运行。

新增配置/CLI可选择策略JSON与成对的参考Parquet/metadata，初始百分位不再强制0.8（仍在首次采样时按映射调整）。普通输出自动增加threshold_trace.csv、classification_policy.json、classification_metadata.json、output_percentile_reference.parquet、output_reference_metadata.json和replay_config.json。新配置文件内相对资源路径按配置目录解析；CLI路径按调用目录解析。导出副本可替代原始资源，不需继续依赖原规则文件。

90项相关测试通过，已跑完整原文CLI：3168完成、0拒绝、全部消耗endpoint，门槛20次变化，平均134.874053ms/P95=296ms。main/replay_config.json配合同次计数导出的full_requests.jsonl逐值复现请求、事件、批次、端点和轨迹；固定80%同输入验证保持不变。结果和验证记录在results/reproduction/dynamic_feature_v2，未覆盖此前产物。历史字段与上一轮一致；对比旧实验只忽略base_priority的整数1/浮点1.0存储dtype，不忽略数值变化。

本次交付没有改变之前压力规则的默认行为，没有实现SLO或输入在线EVT，也没有将跨批诊断接入主引擎。性能指标保留前轮结论：动态相对固定80%没有明显整体收益。详细命令和文件用途见dynamic-output.md。

### 平级动态输出门槛第一轮（2026-10-06）

范围：所有3168条请求基础优先级显式重置为1/normal/default，全部入窗并执行；输入阈值固定40342.5。输出使用同学保存的右连续ECDF参考（5186历史展开样本，754个不同长度），参考文件及metadata哈希验证且运行中不更新。与当前3168完整行数据来自同一底层语料、样本口径不同，不是独立标定/测试集，不宣称真实泛化。

新增配置output_classification=tokens/percentile_fixed/percentile_dynamic（默认tokens），output_percentile_threshold默认0.8。新百分位模式要求batch_scope=all。新增light_first_fifo策略：轻型先，重型后，同类保留到达顺序；这是平级实验规则，不把业务优先级字段用于排序。输出最短参照组仍按输出长度排序。

每个批释放前调用OutputPercentileClassifier，采样当前端点视图、已释放ready数与本批已到达请求数。U=在途/总并发；E=max(0,ready+本批大小-空闲名额)，加入本批用于预估即将释放的已知需求，不包括未来请求。E>=总名额对应critical/60%；否则E>0或U>=85%对应busy/70%；否则U>=60%对应normal/80%；其他idle/90%。这些Enum是压力档位，不改变已经假定全局busy的分支。规则复用runtime.OutputHeavyPolicy，写在config/dynamic_output_policy.json；本轮没有滞回和平滑，不事后调参。

初始控制器状态80%，首次采样可立即改门槛；本次实际使用60/70/90%，80%只为初始状态与固定组门槛。每批仅分类一次，整批共用门槛；已释放请求不随着后续压力变化重新分类。请求到达时百分位分类为pending，arrived事件heavy=null，分类完成后才有最终标签。输入重型OR输出百分位>=门槛为最终heavy。

运行命令：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_dynamic_threshold
```

单独运行动态组：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_dynamic_output.json `
  --output-dir workload_profiling/results/reproduction/dynamic_output_manual_v1
```

固定组可增加--output-classification percentile_fixed --output-percentile-threshold 0.8。CLI新增这两个参数，--batch-order支持light_first_fifo；参考文件需保留data/artifacts/output_percentile_reference.parquet和results/stage2_1/percentile_reference_metadata.json。默认tokens与历史排序仍兼容。

本轮共同设置：突发时间表、批16/超时20ms、额度充足、并发8/12/16、速度20/20/20、无波动、min_rpm，仍批间FIFO。三组：output_shortest按输出升序；fixed_80轻重分组FIFO且80%固定；dynamic同样分组FIFO但门槛随压力更新。固定与动态唯一行为差异是输出门槛更新，不新增跨批。

| 方案 | 全部平均延迟 ms | P95 ms | 最终重型数 | 门槛变化次数 |
| --- | ---: | ---: | ---: | ---: |
| output_shortest | 134.463068 | 295 | 932 | 0 |
| fixed_80 | 134.879419 | 297 | 932 | 0 |
| dynamic | 134.874053 | 296 | 1419 | 20 |

动态改变545条分类、2229条批内位置、2078条延迟（1317改善、761变差）。均值仅降低0.005366ms，尚未优于输出最短，不能声称明显收益。固定80%定义的同一932条长请求均值固定组172.7124→动态171.9496ms；同一2236条短请求119.1100→119.4204ms。不要只比较两组各自的重型均值，因为成员变化。

参考ECDF门槛60/70/80/90%对应最小长度167/257/356/461 tokens。80%是5186历史参考的门槛，不保证当前3168行只命中20%，还受样本口径与ties影响。

结果在results/reproduction/dynamic_output_v1：comparison.md/.json汇总，request_differences.csv比较固定/动态，equal_priority_requests.jsonl可单独以lengths模式复用，arrival_schedule.csv为时间表；output_shortest/fixed_80/dynamic各保存标准日志与threshold_trace.csv、concurrency_wait_snapshots.csv。threshold_trace记录ready、并发、预计超出名额数、压力、前后门槛、token界限和分类数。classification_updated事件与每请求percentile字段可核对全部决策。

60项相关测试通过，三组全量完成无拒绝，重复结果和trace一致，参考文件未变。逐事件确认所有容量等待仍由并发满造成，无额度阻塞。最短输出组的派发/完成时刻与旧tokens分类兼容。新测试workload_profiling.tests.test_dynamic_classification；源码baseline/classification.py。没有实现输入动态EVT、SLO、在线分布更新、平滑/滞回或跨批主排序。

### 批大小能否替代跨批排序（2026-10-06）

命令：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_batch_sizes
```

已执行，正式结果results/reproduction/batch_size_v2；v1为报告校验开发中间目录，不作为最终交接结果。再次运行换--output-dir。脚本读取已校验的priority_burst_321_v1请求CSV长度与业务标签，主BaselineRunner重新回放，不重新tokenize或调用模型。相同突发时间表、3/2/1、原并发8/12/16、额度充足、同速无波动；只改batch_size及折扣，每组batch_wait_ms固定20，仍批间FIFO。

五个大小16/64/128/256/512，各比较无折扣和0.5，共十组全量。全部3168条执行无拒绝，各自重复一致，16条两组CSV与原实验完全一致。时序分解、排序、标签、tokens守恒、窗口超时和只因并发满等待均核对。没有更改主引擎/默认批大小。

同为0.5折扣：

| 批大小 | 全部均值 ms | P95 ms | 平均收集等待 ms | 平均容量等待 ms |
| --- | ---: | ---: | ---: | ---: |
| 16 | 134.9324 | 296 | 0.3280 | 113.2860 |
| 64 | 133.5281 | 294 | 1.3128 | 110.8968 |
| 128 | 131.4015 | 293 | 2.5458 | 107.5372 |
| 256 | 127.9107 | 295 | 5.2528 | 101.3393 |
| 512 | 121.2292 | 284 | 10.3570 | 89.5537 |

批512无折扣132.8074ms→折扣121.2292ms（改善8.72%），比批16仅0.29%明显。同业务组折扣仍有代价：批512 high均值40.4147→50.5649ms。配置512并非每批都是512，10个批中4个按大小、5个超时、1个EOF触发。

跨批诊断16条+折扣仍为104.7500ms/P95=266ms，优于本次大批512折扣121.2292ms/P95=284ms；但这同时涉及收集等待差异，不是同一释放时刻下只改变排序范围的消融。大批的简单性值得考虑，尚无SLO且未比较真实排序计算开销，不能只据均值确定最终方案。

comparison.md和comparison.json为报告，metrics.csv为全部十组指标，cross_batch_reference.json保存与跨批诊断相同配置/数据的校验。各batch_N/no_discount和discount_05保存完整配置、汇总、请求、事件、批、端点和并发快照。acceptance_checks.json保存验收和哈希，arrival_schedule.csv及priority_labels.csv保存共享输入。主要报告中的cross-batch仍是独立诊断，不是已接入主策略。

### 并发压力下折扣收益小的原因诊断（2026-10-06）

检查原并发场景的有无折扣日志：heavy平均服务时间75.9195ms，light为15.1195ms，总服务时间两组均67537ms。1086对顺序被翻转的请求中，1076对是更短的先执行、9对相反、1对相等。因此本次不能主要归因于轻重判定错误。

真正需要检查的是排序范围：198个批均由达到16条触发，平均批收集等待仅0.328ms；所有ready积压峰值457条，但主代码仅每批16条内排序，后批不能越过前批。前批重型即使在本批被排后，仍早于后批轻型，折扣不作用于整个积压队列。

诊断命令（独立模型，不是新主策略）：

```powershell
& .\.venv\Scripts\python.exe -m examples.diagnose_priority_burst
```

完成结果在results/reproduction/priority_burst_diagnosis_v3；重复运行换--output-dir。v1/v2是开发检查中间目录，正式读取v3。该脚本使用已保存的全量请求长度、标签和批释放时刻，不重新计数原文、不读取未来未释放请求；沿用实际端点模型和min_rpm、不抢占运行中请求。local/output两组的派发端点、时刻、服务时间、等待、优先级、批内位置及端点计数与原实验逐值一致，再对local/global、output/service、无折扣/0.5做八组诊断。所有组完整执行、tokens守恒、额度无阻塞，重复重放一致。

单变量对照结果：同为输出长度同分排序，仅批内时无折扣135.3220ms、折扣134.9324ms（改善0.29%）；跨批从所有已释放ready中重新选最高有效优先级时，无折扣115.2317ms、折扣104.7500ms（改善9.10%，P95为289→266ms）。固定无折扣时，local→global均值改善14.85%。因此在本场景，16条批边界明显限制了优先级和折扣作用范围。

把同分排序从输出长度改为准确服务时间，local折扣均值134.9324→134.9233ms，作用很小。跨批折扣场景也只有104.7500→104.5006ms。当前主要问题是队列范围，而非折扣系数不够大或长度代理完全失效。

代价仍需看：global有无折扣的high业务均值31.0610→40.3505ms，low为226.9537→183.1786ms。跨批优先级相对于local显著保护high，却推迟low；折扣又在global内部进行让位。没有SLO/防饥饿，不能把全局排序当作无代价最优方案。主engine仍保留批间FIFO，未擅自接入global。

comparison.md/.json为诊断结果，每个变体保存requests.csv、summary.json、diagnostic_config.json；接受检查在acceptance_checks.json。诊断CSV的端点计数和折扣为重算值，source_batch_position保留原记录位置，dispatch_sequence为诊断实际派发序号。

### 3/2/1 权重实验（2026-10-06）

用户接受重型折扣跨业务等级让位，新增 priority_high_weight、priority_normal_weight、priority_low_weight 配置（默认仍10/3/1），严格要求 high>normal>low>0。只影响缺失标签的合成权重，已有显式权重不覆盖；业务类别生成规则与seed不变。baseline CLI对应--priority-high-weight、--priority-normal-weight、--priority-low-weight。记录字段含权重，实际运行配置可直接用--config复用。

本次高/普通/低为3/2/1、重型折扣0.5。普通轻型2可以先于高重型1.5；普通重型1与低轻型1同分，按输出长度排序。低等级权重不是数值越小越优先的编号，数值越大仍越优先。

运行命令：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_priority `
  --high-weight 3 --normal-weight 2 --low-weight 1 `
  --compare-results workload_profiling/results/reproduction/priority_v1 `
  --output-dir workload_profiling/results/reproduction/priority_weights_321_v1
```

该目录已存在，重复执行请换--output-dir。脚本重新读取全部原始请求，在原额度和额度充足场景中各运行原排序、无折扣优先级、0.5折扣优先级。weight_comparison.json核对业务标签、源数据、请求长度、到达、分类和所有非权重配置一致，再与旧10/3/1折扣组比较；业务标签人数仍为639/1902/627。旧实验配置、权重与产物保留。

新结果目录结构与priority_v1相同，新增weight_comparison.json（旧新权重逐请求与分组对照）、discount_effect.json（新权重下折扣vs无折扣），comparison.md包含两种比较，不能混淆参照组。

相对于3/2/1无折扣，折扣改变141个窗口、1044条请求的批内位置。原额度109条请求延迟改变，33条改善、76条变差，范围-16..16ms；整体平均增加0.131313ms。额度充足30条请求延迟改变，16条改善、14条变差，范围-5..5ms；整体平均减少0.000316ms。P95整体仍分别57328ms与64ms。

相对于旧10/3/1折扣，3/2/1折扣改变973条批内位置；原额度平均增加0.131313ms，额度充足平均减少0.000316ms。额度充足high均值27.472613→27.486698ms，normal29.593586→29.602524ms，low28.012759→27.969697ms。允许跨等级让位确实改变排序，但延迟收益仍非常小，并伴随高/普通组轻微变慢；不能称为明显性能提升。

51项相关测试通过（增加权重变更不改变类别、3/2/1跨等级让位及非法权重检查）。六组均3168条全部执行无拒绝，折扣组重复一致，原排序原有字段与全请求基线一致。该阶段只验证单个seed和固定到达；后续突发并发竞争实验见下一节，尚未验证异构组合，也没有动态阈值或SLO。

### 全请求＋突发＋额度充足：并发竞争下直接折扣对照（2026-10-06）

配置 baseline_priority_burst_321.json，所有请求执行、固定业务标签及3/2/1权重、重型阈值40342.5/578、批16/等待20ms、min_rpm、相同速度20/20/20、波动关闭。只把此前额度充足3/2/1场景的到达方式改为burst：每组最多512条，原始跨度20ms，完整时间表归一化保持0..3167ms。并发仍8/12/16，没有降低上限。两组的唯一差别是heavy_priority_discount=1或0.5。

运行命令：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_priority_burst
```

已运行，结果在results/reproduction/priority_burst_321_v1。comparison.md是直接有无折扣对照，comparison.json保存两组指标、压力核对与逐请求效果。no_discount/和discount_05/均保存完整配置、汇总、请求、事件、批及端点；额外保存concurrency_wait_snapshots.csv。arrival_schedule.csv和priority_labels.csv保存共享输入，request_differences.csv包含全部请求前后差异，acceptance_checks.json保存验收与哈希。再次运行使用--output-dir指定新目录，不覆盖旧产物。

并发压力验证：examples/concurrency_audit.py按日志重建端点计数和ready队列，逐个capacity_wait事件确认所有端点并发达到上限且每个端点RPM/TPM均足以接收队首；如出现额度等待会报错。两组2913条请求产生容量等待，ready峰值457，累计并发全满且ready非空时间为1652/1651ms，额度阻塞事件0。这里的饱和时长按事件间隔累加，不能把capacity_wait事件数量当作请求数量或毫秒。请求的capacity_wait仍可能包含前方队首阻挡。

直接结果（无折扣→0.5）：全部平均延迟135.321970→134.932449ms，降低0.389520ms（约0.29%）；P95均296ms；最大容量等待均333ms。141个窗口和1044条批内位置变化，1359条延迟改善、288条变差，变化范围-9..+8ms。

代价：重型平均190.4985→191.8854ms，轻型平均129.0576→128.4664ms；高优先级重型58条平均163.9483→167.5345ms、P95为328→330ms。普通业务组P95为297→300ms。即使整体均值下降，也不能说明所有业务要求改善；当前没有SLO定义。

两组全部3168条完成无拒绝，业务标签与此前3/2/1实验完全一致；分类、批成员、到达时间表和非折扣参数相同，各自重复回放结果一致。54项相关测试通过（原51项+3项并发核对测试，覆盖真实并发等待、识别额度等待、空输入）。新增测试名workload_profiling.tests.test_concurrency_audit。

结论仅限本数据、单seed、此突发强度和窗口内排序：已排除额度等待干扰，观察到小幅平均延迟改善，整体P95未改善，并且部分重型/高优先级请求变慢。不能因此宣称折扣普遍有效或无效。未增加跨窗口重排、降低并发、动态阈值、SLO或异构端点组合。

### 折扣额外效果精确核对（2026-10-06）

本段只比较 priority_no_discount 与 priority_discount，不把业务优先级本身当作可省略的需求。两场景均有22个窗口、123条请求的批内位置变化。原额度下111条请求的端点改变、8条派发时间与延迟改变（6条改善、2条变差，范围-2到+6ms），总体及业务组平均延迟精确不变；额度充足下119条端点改变，但派发时间和延迟全都不变。

原因：10/3/1配0.5保持跨业务等级顺序；同级输出重型本就被短输出优先排后，额外重排主要涉及41条仅输入重型请求。当前端点速度相同，改变端点不直接改变服务时间；容量允许同一虚拟时刻派发时，排序变化不产生时间变化。窗口间仍按FIFO，原额度长等待亦非本次折扣能消除。没有发现本场景下折扣的额外整体延迟收益；不能据此断言所有配置下无效，也不应为显示收益而事后挑参数。

- docs/project-goals.md：项目目标与研究范围。
- docs/service-jitter.md：耗时波动原理与运行方式。
- docs/endpoint-speed.md：端点速度差异场景。
- docs/burst-arrivals.md：突发时间表与运行方式。
- docs/running.md、architecture.md、results.md：通用运行、引擎语义、结果字段。
