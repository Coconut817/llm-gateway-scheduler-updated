# 架构、数据与调度语义

[返回首页](../README.md) · [运行方式](running.md) · [Python API](api.md)

## 目录职责

表中的 `data/`、`results/`、`cache/` 均位于 `workload_profiling/` 下。

| 路径 | 内容 | 下载后是否必须存在 |
| --- | --- | --- |
| `baseline.py`、`demo.py` | 命令行、网页入口 | 是 |
| `docs/`、`examples/` | 文档与小型可运行示例 | 是，推荐随代码发布 |
| `workload_profiling/baseline/` | 读取、重型判断、收集批、路由、模拟完成、导出 | 是 |
| `workload_profiling/common/` | 固定 tokenizer、消息转换、路径、数据校验 | 是 |
| `workload_profiling/runtime/` | 当前请求计数及 sender 接口，独立于模拟调度器 | 是 |
| `workload_profiling/config/` | baseline 及旧百分位策略配置 | 是 |
| `workload_profiling/stages/` | 历史离线分析入口 | 复现历史实验时使用 |
| `prompt数据/` | 默认 3,168 条 prompt/response 原始记录 | 默认 prompt 回放需要；可以用 `--source` 替代 |
| `实验数据2/` | 历史网关流量、价格、容量配置 | baseline 不读取 |
| `data/tokenizer/Qwen3-8B/` | 下载并校验的 tokenizer/config/template，无权重 | prompt 计数需要，首次自动构建；Git 忽略 |
| `data/processed/` | 唯一长度基表与两份标签 Parquet | 历史分析、旧长度页面使用；长度 baseline 不需要 |
| `data/artifacts/` | 冻结 output ECDF reference | 旧百分位策略使用；新 baseline 不需要 |
| `data/exports/` | 历史 14/18/23 列 CSV 副本 | 可选导出，Git 忽略 |
| `results/stage1/`、`stage2/`、`stage2_1/` | 历史统计、拟合、报告和 metadata | 历史实验来源记录 |
| `results/baseline/` | CLI 默认运行产物与网页的 `web/<run_id>/` | 本地生成，Git 忽略 |
| `cache/` | smoke、库缓存、临时测试产物 | 可重建，Git 忽略 |

整理只移动了原根目录的两份历史 CSV 到 `data/exports/`。原始 JSONL、Parquet、固定 tokenizer 和历史科学结果的内容及默认路径保持不变。`requirements.txt` 引用包内固定依赖清单，避免维护两个版本列表。

## 三种请求口径

| 流程 | 一行表示什么 | 是否展开历史 assistant |
| --- | --- | --- |
| baseline `--source-format prompt` | 原始 `prompt + response` 对应的一次完整上下文调用 | 否 |
| 历史 Stage 1 | 一个包含历史和外层 response 的原始行，枚举合格 assistant 样本 | 是 |
| `runtime.RequestProcessor` | 当前要计数或发送的一次请求，调用方提供完整上下文 | 否 |

原始数据说明见 [Prompt 数据说明](../prompt数据/prompt回答数据包_3168条/使用说明.md)。baseline 默认为 3,168 个请求，Stage 1 当前为 5,186 个有效展开样本。文件行号只是定位键，不证明不同行属于独立业务会话。

## 完整上下文的处理

baseline 逐行读取 UTF-8/UTF-8 BOM JSONL，取 `row["prompt"]["messages"]`，保留角色顺序，规范化消息后交给固定 Qwen chat template；存在时传入 `tools`、`tool_choice`、`parallel_tool_calls`。后两项是 API 控制参数，模板是否渲染各字段由原版模板决定。

- system、历史 user、历史 assistant、tool 和最后一条提问均属于 input。
- 外层 `row["response"]` 单独计为当前 output，不拼进 input。
- input 包含 chat template 的角色标记及 generation prompt；output 不添加角色标记、特殊 token 或 EOS。
- 只计回答文本，不把结构化 tool_calls 或独立 reasoning_content 算入 output。历史 reasoning 的渲染遵循模板本身的规则。
- 不截断、不填充；超过 tokenizer 的标称最大上下文长度仍只计数，不执行模型。
- `type=text` content 列表按原顺序拼接；未知非文本块报错。空字符串 response 为 0 tokens，缺失/null response 不会被假设为 0。
- 原始 `messages` 以 assistant 结尾的行仍原样计数。baseline 不自行推断它应被删除或用作外层 response。

固定 tokenizer 是 `Qwen/Qwen3-8B`，revision 为 `b968826d9c46dd6066d109eabc6255188de91218`。只下载 tokenizer/config/template 白名单，随后校验本地缓存。长度是统一离线代理，不保证等于不同生产模型的计费 usage。

## 虚拟时钟与重型分类

默认 fixed 模式第 i 个请求在 `i * arrival_interval_ms` 到达，第 0 条在 0ms。实现按行惰性消费原文；到达间隔属于虚拟模型，不是每隔 1ms 墙钟 sleep。tokenization、绘图或磁盘耗时不改变模拟到达时间。

可选 burst 模式预读取全部请求并构建确定性到达时间表，保持首末时刻和请求顺序相同，允许同一毫秒多条请求。实际间隔不固定，标称 interval 仅确定总跨度和 EOF。详见 [突发到达](burst-arrivals.md)。

```text
input_heavy  = input_tokens  >= input_threshold_tokens
output_heavy = output_tokens >= output_threshold_tokens
heavy        = input_heavy OR output_heavy
```

默认阈值 40342.5 和 578 是操作参数，采用旧展开样本的 P95 数值作为起点。它们不是原始 3,168 行数据的重新估计结果，也不是稳定 EVT onset。设某轴阈值为 0 会让该轴全部命中。

上述为默认output_classification=tokens模式。新百分位模式要求所有请求入窗：固定输入阈值，批释放前用冻结输出ECDF与本批门槛分类；percentile_fixed门槛固定，percentile_dynamic按当前并发/已到达需求调整。分类被冻结，不在等待中改标签。light_first_fifo仅稳定分组轻先重后，组内FIFO。见 [交接说明](handoff-guide.md)。

分类前使用已记录回答的输出长度，模式名为 `oracle_recorded_response`。这使基线可比较已知工作量的调度行为；发送前的真实输出预测并未实现，接入边界见 [扩展指南](extensions.md)。

## 收集批与执行队列

默认 batch_scope=heavy_only 时，轻型请求在到达时完成，延迟 0，不占用模拟端点，只有重型进入收集队列。新 batch_scope=all 模式下，所有轻重请求进入同一收集队列并消耗端点容量，分类标签仅用于统计，尚无优先级折扣。两种模式均满足任一条件释放：

1. 队列达到 `batch_size`，原因 `batch_size`。
2. 最老请求等待达到 `batch_wait_ms`，原因 `timeout`；即使没有新请求到达也按时触发，新到达不会重置计时器。
3. 原始输入结束，原因 `end_of_input`；最后不足一批也会处理。EOF 在下一次预定读取时发现，即最后到达加一个到达间隔。

释放时先调用 `order_batch(requests, endpoints, now_ms)`，得到本批请求的派发顺序，再逐条调用 `select(request, candidates, now_ms)` 选择端点，每分配一条立即更新状态再选择下一条。两种策略可以独立替换；这不是模型 API 的张量 batch 或一次多 prompt 调用。

默认 batch_order=fifo，保持到达顺序；shortest_first/longest_first 分别按 input+output 总 tokens 升序/降序，同长保留到达顺序。旧 JSON 配置缺少 batch_order 时仍为 fifo。

排序输入为冻结请求 tuple 和所有端点的冻结快照（包含忙碌端点）；返回完整 request_id 排列。排序时不预留容量，路由时才根据最新状态筛选可用端点。排序每次释放只调用一次，容量等待期间不重新调用。返回漏项、重复、陌生 ID 或非法类型会报错。

排序后的批按释放先后追加到容量等待队列，后批不能越过前批。队首容量不足时等待，后续请求不会越过队首。`batch_wait_ms` 约束收集阶段，容量等待可能更长。相同时刻按“完成 → 到达 → 超时 → 调度”处理；到达凑满批与超时重合时，记录 `batch_size`。

`batches.json` 的 request_ids 保留到达顺序、dispatch_order 记录排序后的计划顺序；请求行的 batch_position 为 0-based 计划位置。结果 CSV 本身仍按输入到达顺序排列。具体接入见 [排序与路由扩展](extensions.md)。

## 端点状态与策略

默认三个端点都是模拟端点：

| 端点 | RPM 上限 | TPM 上限 | 并发上限 |
| --- | ---: | ---: | ---: |
| endpoint_a | 600 | 5,000,000 | 8 |
| endpoint_b | 900 | 7,500,000 | 12 |
| endpoint_c | 1,200 | 10,000,000 | 16 |

端点维护 `(t-60000,t]` 滚动窗口。调度一次：请求数加 1、TPM 预留完整 input+output token 数、并发加 1。完成一次：仅并发减 1；RPM/TPM 在相应调度记录达到 60 秒时过期。

候选端点须同时满足：

```text
requests_in_window < rpm_limit
tokens_in_window + request.total_tokens <= tpm_limit
concurrency < concurrency_limit
```

内置策略按 `requests_in_window / rpm_limit` 最小选择，同分按端点配置顺序。TPM 和并发参与容量保护，同时作为只读状态提供给后续策略。调度评分只负责选择，不能修改计数或绕过容量限制。

模拟完成时间由端点参数计算：

```text
service_ms = max(1, ceil(base_latency_ms
                        + input_tokens / input_tokens_per_ms
                        + output_tokens / output_tokens_per_ms))
```

默认为 5ms 基础延迟、输入 2000 tokens/ms、输出 20 tokens/ms。所有上限和速度均可修改。容量不足时虚拟时钟推进到完成或窗口过期事件；请求总 tokens 超过所有端点 TPM 上限时明确拒绝，避免永久等待。

## 模块间关系

`baseline` 使用长度阈值、已记录输出和模拟端点；`runtime.OutputHeavyPolicy` 使用冻结历史 ECDF、百分位阈值和外部拥塞枚举。二者独立，新 baseline 不自动读取旧 Heavy 标签或拥塞阈值映射。

模拟引擎只持有 `WorkloadRequest` 的定位键与长度；原文处理完成后不存入调度对象。真实 sender 接口在 `runtime`，并没有自动连到 baseline 的批队列、端点容量和完成事件。具体接口见 [Python API](api.md)。
