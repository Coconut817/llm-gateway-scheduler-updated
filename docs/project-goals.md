# 项目目标与基线对齐

2026-10-07动态功能已完成普通CLI交付：规则可配置、自动轨迹与资源快照、独立复现配置，全量原文及资源快照重放通过。详见dynamic-output.md与handoff-guide.md。当前交付是功能与可复现性，未声称性能提升；SLO仍未实现。

## 目标一：调度策略优化（核心）

2026-10-06 最新对齐：公司无既定业务优先级，保留优先级输入接口；主线改为所有请求基础优先级=1的平级调度。跨批、大批和重型折扣保留为候选，不围绕合成高/普通/低标签寻找收益。已完成dynamic_output_v1的平级动态输出门槛第一轮，机制生效但平均收益几乎为0，未优于输出最短参照。以前混合优先级结果保留。具体范围与复现注意事项见handoff-guide.md。

在调度前已知 input/output token 长度的设定下，研究重型请求的批内排序与 endpoint 路由。复现交接基线“输出 token 最短优先 + 最小 RPM 占比路由”，再以相同数据和环境比较新策略。保留现有重型判定作为起点。

目前先记录重型请求平均/P95 延迟、排队时间、拒绝数及端点分配。SLO 的具体时限尚未确定，不能将延迟指标直接称为 SLO 达标率。

## 目标二：模拟环境改进（后续）

在保留原始场景的基础上，增加可复现的到达模式与 endpoint 性能差异，必要时增加时延波动。涉及随机性时固定种子。比较策略时使用一致场景，分别报告原始环境与扩展环境的结果。

## 原始交接基线对齐（历史状态）

- 仓库默认：fifo + min_rpm；内置 shortest_first 按 input+output 排序。
- 交接描述：仅按 output tokens 升序，再 min_rpm；使用 examples.output_shortest_first:OutputShortestFirstOrder 复现，稳定保留同长请求的到达顺序。
- 到达间隔为 1ms 虚拟时间；批大小 16、等待 20ms。
- 三个模拟端点容量不同，基础时延和输入/输出速度相同。服务时间随长度变化，并非恒定。
- 原heavy_only基线轻型立即完成且不占容量；现在新增all模式，当前主线所有请求均执行。
- 原baseline与runtime输出百分位独立；现已在可选百分位模式接入，默认tokens仍保留。
- 原始 prompt 每行一次请求；历史 Stage 1 展开样本不可替代原始请求口径。

## 复现命令

从项目根目录执行（python 指可用 Python 环境）：

```powershell
python baseline.py --source examples/length_requests.jsonl --source-format lengths --output-dir workload_profiling/results/alignment/default_fifo
python baseline.py --source examples/length_requests.jsonl --source-format lengths --batch-order examples.output_shortest_first:OutputShortestFirstOrder --output-dir workload_profiling/results/alignment/handoff_output_shortest
```

两次运行除排序外参数相同，各目录保存配置、来源哈希、summary、请求、事件、批次和端点记录。小型数据只验证链路，不足以证明策略优劣。

2026-10-05 已完成原始 3168 条请求的全量交接基线回放，结果在 results/reproduction/handoff_v1；323 条重型请求，拒绝 0，容量等待 0。固定 tokenizer 已下载。每行一次请求，不使用历史展开长度表替代。

服务时间波动支持默认关闭和固定种子，说明与全量对照见 [服务时间波动](service-jitter.md)。

新增独立的端点输出速度配置 A/B/C=30/20/10 tokens/ms，耗时波动关闭；全量对照命令与结果说明见 [端点速度实验](endpoint-speed.md)。

初期新增确定性突发到达模式，保持完整请求顺序与首末到达时刻；见 [突发到达](burst-arrivals.md)。此阶段三个环境因素分别验证，后续组合与排序试验见下文。

2026-10-06 按右侧繁忙分支补齐所有请求入窗执行，新增 batch_scope=all，保留旧模式。全量新基线在 results/reproduction/all_requests_v1/all_requests/；旧环境实验均为 heavy_only，不可直接当作新模型的策略对照。统一修改记录、命令、结果与待完成事项维护在 [交接使用说明](handoff-guide.md)。

同日初期新增基础业务优先级和固定阈值重型折扣排序，结果在 results/reproduction/priority_v1。原额度、额度充足两场景各比较原排序/无折扣优先级/0.5折扣优先级，均全量完成。标签是合成实验数据，不代表真实业务；当时尚未接入动态百分位，后续已完成；输入EVT接入与SLO仍待实现。

2026-10-07文档整理：当前状态与相对原项目的修改集中在handoff-guide.md，逐次历史记录保存在change-history.md，新增handoff-start.md给接手同学按步骤运行。算法、配置和已有实验数据未因文档整理变化。

同日增加可配置业务权重，完成3/2/1与10/3/1的全量对照，保留相同标签、0.5折扣和环境。结果在 results/reproduction/priority_weights_321_v1：排序变化更多，整体延迟变化仍极小；具体代价与运行命令见交接说明。默认权重仍10/3/1。

同日完成全请求+突发+额度充足的直接3/2/1折扣对照，结果在 results/reproduction/priority_burst_321_v1。逐事件验证所有容量等待来自并发满、无RPM/TPM额度阻塞。整体平均延迟改善约0.29%，P95不变，高优先级重型平均延迟上升；小幅收益与代价同时记录，不宣称普遍提升。

随后独立诊断支持批间FIFO为本场景小收益的重要限制：跨批选择ready后，同一有无折扣均值改善9.10%，P95为289→266ms，同时有业务组代价。结果在results/reproduction/priority_burst_diagnosis_v3，主策略未接入跨批选择。

批大小16/64/128/256/512的主引擎全量对照已完成，固定20ms超时、相同突发与额度。批512有无折扣改善8.72%，同时增加收集等待；结果在results/reproduction/batch_size_v2。默认批仍16，尚未依SLO或真实计算开销确定方案。
