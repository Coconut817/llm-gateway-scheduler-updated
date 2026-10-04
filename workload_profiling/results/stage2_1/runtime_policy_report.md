# Stage 2.1：Output Percentile 与动态 Output-Heavy

当前系统只定义 **congestion-consumer interface**，不声称已经实现 congestion estimation。
本阶段使用固定历史 reference 和外部状态调整阈值。没有新增 RPM/TPM 计算、拥塞 score、容量模拟、拥塞时间线、hysteresis、路由或调度。

## 1. output_percentile 如何计算？

采用右连续经验分布函数（`empirical_cdf_right`）：

`output_percentile(x) = count(reference.output_tokens <= x) / N`

相同长度的全部请求具有相同百分位，不按行顺序拆分 ties。短于历史最小值时为 0，达到或超过最大值时为 1。它是历史长度排名，不是 EVT tail probability；也不是新请求输出长度的预测器。调用方须提供已知的 `output_tokens`。

## 2. reference distribution 来自哪里？

来自 Stage 1 `workload_profiling/data/processed/request_lengths.parquet` 的全部 **5,186** 条有效 request 的 `output_tokens`，含 **754** 个不同长度。历史 assistant 和 top-level response 均按 Stage 1 成功样本原样纳入，每条 request 权重相同。

冻结 artifact：`workload_profiling/data/artifacts/output_percentile_reference.parquet`，保存每个长度的频数、累计频数、ECDF。metadata 记录输入及 artifact 的 SHA-256、样本数、ties 和边界规则。`PercentileReference.load()` 验证校验和与样本数。运行时仅查这个 reference，状态切换和人工调阈值都不会更新分布。脚本仅用于显式离线构建；以后如更换历史基线，应单独管理新的 reference 版本。

长度继承 Stage 1 的 Qwen/Qwen3-8B 离线 tokenizer 定义，不声称是生产模型的真实 token 数。

## 3. 默认 threshold 为什么是 0.95？

默认 **0.95** 是用户指定的 operational parameter，表示选择历史输出较长的约 Top 5%，不是从数据估计出的自然 Tail Onset，也不是解决 Output EVT 不稳定的方法。

当前 reference 下最短命中长度为 **578 tokens**；共 **263/5,186** 条命中，占 **5.071346%**。边界长度有 **6** 条且一起命中，因此 ties 使比例不必恰好是 5%。Stage 2 原有严格 `output_tokens > P95` 标签命中 **257** 条；两者定义不同。

## 4. Output-Heavy 如何定义？

`output_heavy = output_percentile >= output_heavy_threshold`

输出数据 `workload_profiling/data/processed/output_labels.parquet` 只保存 request 键和以下新增标签： `output_percentile`、`output_heavy`、`output_heavy_threshold`、`threshold_source`、`congestion_state`。本文件是 **DEFAULT** 阈值快照，`congestion_state` 全部为空，因为没有外部运行状态。通过 `common.datasets.load_dataset('stage2_1')` 与唯一长度基表、Stage 2 标签关联得到完整视图。以后动态判断必须重新调用 policy，离线快照不会自动改写。

原有 `output_tail_evt` 的 **5,186** 个空值原样保留：Stage 2 未找到稳定 Output EVT 阈值。本阶段的 Output-Heavy 是新的操作性标签，不会把缺失的 EVT 结果填成 false 或伪造阈值。

`policy.evaluate(output_tokens)` 返回已知长度、百分位、布尔标签、生效阈值、外部状态及来源。`classify(output_percentile)` 是低层接口；只有百分位输入时 `output_tokens` 为 null，也可显式传入长度；正常完整流程使用 `evaluate()` 从长度计算百分位，避免调用方拼接不一致的两个值。

## 5. threshold 如何人工修改？

`policy.set_threshold(0.85)` 开启人工覆盖，`threshold_source=MANUAL`。`policy.get_threshold()` 查询生效阈值；`policy.clear_manual_override()` 解除覆盖并使用当前 provider 的状态，或无 provider 时恢复最近显式推送的状态，完全没有外部状态则恢复 DEFAULT。

优先级为 **MANUAL > CONGESTION > DEFAULT**。覆盖期间仍可记录外部状态，但外部状态不能改变人工阈值。阈值必须是有限数并满足 `0 < threshold < 1`；非法输入抛出 ValueError，已有合法覆盖保持有效。

## 6. congestion 如何通过接口修改 threshold？

外部 `CongestionProvider.get_state()` 返回 `CongestionState`。每次判断读取一次状态，用集中配置查阈值：

| 外部状态 | 阈值 |
| --- | --- |
| IDLE | 0.98 |
| NORMAL | 0.95 |
| BUSY | 0.90 |
| CRITICAL | 0.80 |

配置源为 `workload_profiling/config/runtime_policy.json`，运行时可指定保存的 `runtime_policy_config.json`。没有 provider 时也可通过 `policy.update_from_congestion(CongestionState.BUSY)` 显式推送外部状态；绑定 provider 时，以每次读取到的 provider 状态为准。

契约测试中的百分位 0.92 在 NORMAL 下为非 Heavy，BUSY 下为 Heavy。该数值仅是测试输入，不对应捏造的真实请求。

## 7. 为什么当前没有实现 RPM/TPM 拥塞计算？

当前阶段没有可用于真实估计的 RPM、TPM、Concurrency、Burst usage/capacity 数据。历史 response 长度只用于长度排名，不能据此伪造利用率、容量或状态时间线。本模块消费外部状态，不判断何时进入/退出 BUSY，也不实现 hysteresis。

## 8. StaticCongestionProvider 的作用是什么？

它是测试和接口验证工具，可以人工 `set_state(CongestionState.BUSY)`，检查映射、人工覆盖和恢复行为。它不是拥塞估计算法，没有容量、分数或时间线。本阶段生成的默认数据集没有调用它来制造状态。

## 9. 以后如何接入真实 congestion controller？

由未来真实 controller 实现 `CongestionProvider`，在 `get_state()` 返回有效的 `CongestionState`，再通过 `OutputHeavyPolicy(reference, congestion_provider=provider, config_path=...)` 注入。真实指标采集、状态估计及 hysteresis 属于未来 controller，当前只保留抽象接口。冻结 reference 和现有策略接口可继续使用。

## 验证与复现

实际执行 **23** 个单元测试，全部通过。另完成 artifact 保存/加载往返、全量 ECDF 与直接累计计数一致性、标签一致性、Stage 2 原字段保留、历史 source 文件 SHA-256 不变等 **7** 项验收检查，详见 `runtime_policy_test_results.json`。

```powershell
& .\.venv\Scripts\python.exe -m workload_profiling.stages.stage2_1.run
& .\.venv\Scripts\python.exe -m unittest workload_profiling.tests.test_runtime_policy
```

本阶段完成后停止，未进入新的 Tail Detection、真实拥塞估计或调度阶段。
