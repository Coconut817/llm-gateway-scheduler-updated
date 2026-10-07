# 原项目改动汇总与交接索引

维护日期：2026-10-07。本文维护当前状态和主要修改，不把历史实验方案当作已确定的最终策略。

- 接手运行先读：[接手使用说明](handoff-start.md)。
- 动态规则与复现详解：[动态功能使用说明](dynamic-output.md)。
- 之前逐次追加的完整记录已保留：[历次修改与实验档案](change-history.md)。

## 当前交付

这是离线、虚拟时钟的调度模拟框架。模型已确定，实验提前知道输入/输出长度，来自已有prompt/response或长度JSONL。三个endpoint是模拟服务，不调用真实模型。

当前主线是平级调度：无业务标签时基础优先级默认1。优先级接口保留，合成高/普通/低标签只用于历史实验。

当前入口配置是 `workload_profiling/config/baseline_dynamic_output.json`：所有请求入窗并执行；固定输入阈值40342.5，输出按冻结ECDF及压力门槛分类；轻型先、同类FIFO；按min_rpm选择有容量端点。主引擎批间仍FIFO。

普通 `python baseline.py` 仍读取原 `baseline.json`，默认heavy_only：轻型零耗时完成，只有重型消耗端点。运行全请求功能必须选新配置或设置 `--batch-scope all`。

## 相对原项目的主要改动

| 模块 | 原来已有 | 本次新增/调整 | 主要文件 |
| --- | --- | --- | --- |
| 请求范围 | 仅重型入窗，轻型直接完成 | all模式轻重同窗、都派发消耗容量，保留旧模式 | [config.py](../workload_profiling/baseline/config.py)、[engine.py](../workload_profiling/baseline/engine.py) |
| 到达 | 固定间隔虚拟到达 | fixed/burst；突发保持请求数、顺序、首末时刻 | [arrivals.py](../workload_profiling/baseline/arrivals.py)、engine.py |
| 处理耗时 | 基础时延＋输入/输出速度公式 | 稳定哈希产生可复现波动，默认关闭；新增不同速度配置 | [models.py](../workload_profiling/baseline/models.py)、baseline_endpoint_speed.json |
| 优先级 | 没有业务字段 | 可选基础权重接口、合成标签、权重配置及重型折扣 | [priority.py](../workload_profiling/baseline/priority.py)、models.py、[source.py](../workload_profiling/baseline/source.py) |
| 批内排序 | FIFO、总tokens长短排序及扩展接口 | 仅输出最短示例、effective_priority、light_first_fifo | [ordering.py](../workload_profiling/baseline/ordering.py)、[output_shortest_first.py](../examples/output_shortest_first.py) |
| 轻重分类 | baseline固定token阈值；runtime已有ECDF/策略模块 | 将ECDF及输出策略接入baseline；固定/动态百分位；按批分类并冻结标签 | [classification.py](../workload_profiling/baseline/classification.py)、engine.py、runtime/output_heavy_policy.py |
| 压力反馈 | 外部拥塞接口，未自动接baseline | 并发＋ready＋本批已到达需求映射门槛，JSON配置和校验 | [dynamic_output_policy.json](../workload_profiling/config/dynamic_output_policy.json)、classification.py |
| CLI及复现 | 基本回放、策略注入和日志 | 新模式参数；自定义规则/参考；自动副本与replay_config | [run.py](../workload_profiling/baseline/run.py)、[reporting.py](../workload_profiling/baseline/reporting.py) |
| 统计 | 重型队列/延迟、事件和端点计数 | 全部/轻型/业务组指标、执行数、每请求门槛与压力轨迹 | engine.py、reporting.py |
| 研究验证 | 原有测试 | 全量对照、并发原因核对、排序范围/批大小诊断、新行为测试 | examples/、workload_profiling/tests/ |

min_rpm本身没有替换：仍从RPM/TPM/并发满足条件的候选中选择RPM占比最小者。容量窗口、虚拟完成事件及排序/路由扩展接口是原项目已有能力，本次复用了它们。历史科学分析和原始数据未重算，依赖版本清单未更换。

## 功能状态

| 状态 | 内容 |
| --- | --- |
| 主引擎已实现 | 全请求执行、固定/突发到达、耗时波动、优先级接口、批内折扣排序、固定/动态输出百分位、日志及独立复现资源 |
| 场景/实验已完成 | 端点速度差异、额度充足、混合优先级、不同批大小、平级动态门槛 |
| 仅独立诊断 | 跨批选择所有已释放ready；服务时间作同分依据。主engine未接入跨批选择 |
| 原项目已有但未接入新调度 | 输入EVT分析、历史分布分析、同步sender接口 |
| 未实现 | 输入在线EVT、SLO配置/达标统计、流式TTFT/TPOT事件、非繁忙池、健康/Cooldown、价格/Pareto、真实异步执行、跨批防饥饿、动态平滑/滞回 |

当前输入40342.5是历史P95起点，不是自动使用EVT约3835.6。输出百分位模式不使用绝对578；tokens模式才使用它。长度相关SLO预算仅讨论过，未实现。

## 配置选择

均位于 `workload_profiling/config/`。前两项适合当前接手；其余用于旧行为或历史实验。

| 配置 | 范围 | 到达 | 排序/分类 | 用途 |
| --- | --- | --- | --- | --- |
| [baseline_dynamic_output.json](../workload_profiling/config/baseline_dynamic_output.json) | all | burst | 轻重分组FIFO、动态百分位 | 平级动态入口，额度充足 |
| [baseline_all_requests.json](../workload_profiling/config/baseline_all_requests.json) | all | fixed | 输出最短、固定tokens | 全请求原额度基线 |
| [baseline.json](../workload_profiling/config/baseline.json) | heavy_only | fixed | FIFO、固定tokens | 兼容原项目默认 |
| baseline_endpoint_speed.json | heavy_only | fixed | 输出最短、30/20/10速度 | 历史速度实验 |
| baseline_burst.json | heavy_only | burst | 输出最短 | 历史到达实验 |
| baseline_priority.json | all | fixed | 合成标签、折扣排序 | 原额度优先级实验 |
| baseline_priority_ample_quota.json | all | fixed | 合成标签、折扣排序 | RPM/TPM乘100 |
| baseline_priority_burst_321.json | all | burst | 3/2/1折扣排序 | 历史并发竞争实验 |

额度充足仅把RPM/TPM乘100。动态预设仍有8/12/16共36个并发名额、输出速度20/20/20、波动0。这些是模拟参数，不是生产测量值。

## 正式结果索引

路径相对 `workload_profiling/results/reproduction/`，报告在各目录comparison.md或report.md。

| 目录 | 问题 | 解读 |
| --- | --- | --- |
| handoff_v1/ | 原交接输出最短＋min_rpm | 3168逻辑完成，仅323条消耗端点 |
| service_jitter_v1/ | ±20%波动 | 旧heavy_only场景，功能可复现 |
| endpoint_speed_v1/ | 30/20/10速度 | 旧heavy_only，环境变化而非策略提升 |
| burst_arrivals_v1/ | 突发 | 旧heavy_only，97条容量等待 |
| all_requests_v1/ | 全请求执行 | 原额度产生分钟窗口等待 |
| priority_v1/ | 10/3/1有无折扣 | 合成标签，不是平级结论 |
| priority_weights_321_v1/ | 3/2/1权重 | 固定到达下折扣收益小 |
| priority_burst_321_v1/ | 并发竞争下折扣 | 无额度阻塞，平均改善约0.29%，有重型代价 |
| priority_burst_diagnosis_v3/ | 排序范围诊断 | 跨批有无折扣115.23→104.75ms，约9.1%；混合优先级诊断，未接主代码 |
| batch_size_v2/ | 批16/64/128/256/512 | 大批增加排序范围及收集等待，未确定最优值 |
| dynamic_output_v1/ | 平级最短输出/固定80%/动态 | 机制生效，平均收益几乎0，未胜过输出最短 |
| [dynamic_feature_v2/](../workload_profiling/results/reproduction/dynamic_feature_v2/) | 正式CLI/资源复现 | main原始全量、replay副本重放、fixed_80固定模式 |

batch_size_v1、priority_burst_diagnosis_v1/v2是开发中间结果，使用上表最终目录。不同工作量、标签和环境的均值不能串成策略进步曲线。

最新平级实验：输出最短134.4631ms/P95=295；固定80%分组FIFO134.8794/297；动态134.8741/296。动态改变545条分类，均值只降低约0.005ms。功能可运行、可复现，不代表已证明性能优势。

## 验证与维护

2026-10-07已完成90项相关测试。完整原文CLI3168条全部执行、0拒绝；导出资源重放的请求、事件、批、端点计数和轨迹一致。验收与哈希在 [acceptance_checks.json](../workload_profiling/results/reproduction/dynamic_feature_v2/acceptance_checks.json)。

接手运行与最小验证见handoff-start.md。对照脚本的基础结果依赖见examples/README.md；不用为了试运行先重跑昂贵Stage 2。

后续维护：

1. 行为变更更新本文状态，change-history.md追加日期、目的、文件和验证。
2. 实验新建配置/目录，记录实际参数、数据/资源哈希、参照及局限，不覆盖旧结果。
3. 使用入口变更同步handoff-start.md与dynamic-output.md，确认命令可运行。
4. 诊断脚本、主代码与待实现功能分开标记。

本次整理将旧handoff-guide全文保存为change-history.md，当前文件作为改动总览，另新增接手运行说明和实验索引。仅整理文档，没有改变算法、配置或历史科学数据。

按接手说明实际运行9条长度样例，9条全部派发完成、拒绝0，5条轻型均消耗端点；结果在results/reproduction/handoff_smoke。本次核对使用入口与文档链接，90项测试是前次功能交付记录，不冒充新的性能实验。
