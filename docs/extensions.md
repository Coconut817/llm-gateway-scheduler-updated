# 自定义策略、输入与执行器接入

[返回首页](../README.md) · [Python API](api.md) · [可运行示例](../examples/README.md)

## 排序与路由是两个独立接口

```text
重型批释放 → order_batch(整批请求, 所有端点快照, 当前时间)
           → 按返回顺序进入容量等待队列
           → select(当前请求, 可用端点快照, 当前时间)
           → 更新端点状态 → 下一条请求
```

`batch_order` 决定先调度谁，`strategy` 决定当前请求去哪个 endpoint。两者可以独立配置，默认组合为 `fifo + min_rpm`。旧配置不写 batch_order 时自动采用 fifo。

## 替换批内排序

实现同步 `order_batch(requests, endpoints, now_ms)`，返回本批全部 request_id 的 list/tuple 排列。无需继承框架类：

```python
from workload_profiling.baseline import EndpointView, WorkloadRequest

class MyOrder:
    def order_batch(self, requests: tuple[WorkloadRequest, ...],
                    endpoints: tuple[EndpointView, ...], now_ms: int) -> list[str]:
        # 示例：录制输出越长，越早派发；同长时保持到达顺序
        ordered = sorted(requests, key=lambda r: r.output_tokens, reverse=True)
        return [request.request_id for request in ordered]
```

| 输入/输出 | 契约 |
| --- | --- |
| requests | 本批非空 tuple，按到达顺序；成员是冻结 WorkloadRequest，可用 input_tokens/output_tokens/total_tokens |
| endpoints | 所有配置端点的冻结快照，包括当前繁忙端点；不是为某条请求筛选后的候选列表 |
| now_ms | 批释放时的虚拟时间，不是系统墙钟时间 |
| 返回值 | request_id 字符串序列，例如 list 或 tuple；与本批成员完全相同，每个 ID 恰好一次 |

漏项、重复、陌生 ID、非字符串元素或非序列返回值会使回放失败，不会静默修正或丢请求。排序输入不可修改；端点计数由引擎维护。

每次非空批释放只调用一次排序，batch_size、timeout、end_of_input 三种触发都支持。排序后不在容量等待期间重新排序；后续批按释放顺序追加，不能越过前批。排序看到释放时的状态，不包含尚未派发请求的未来预留量。

### 内置排序

| 规格 | 规则 |
| --- | --- |
| `fifo` | 保持到达顺序，默认 |
| `shortest_first` | input+output 总 tokens 升序，同长保持到达顺序 |
| `longest_first` | input+output 总 tokens 降序，同长保持到达顺序 |

这里的“长短”是离线工作量代理，不等于预测服务时间；模拟输入与输出速度不同。需要服务时间评分时自行实现策略。

### 自定义排序与路由一起运行

随仓库的 [OutputLongestFirstOrder](../examples/output_first.py) 与 [MinTpmStrategy](../examples/min_tpm.py) 可一起使用：

```powershell
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --batch-size 2 --batch-wait-ms 3 --batch-order examples.output_first:OutputLongestFirstOrder --strategy examples.min_tpm:MinTpmStrategy --output-dir workload_profiling/results/baseline_custom/output_first
```

自己的文件放在可导入路径中，分别使用 `--batch-order my_order:MyOrder` 和 `--strategy my_route:MyRoute`。两个加载器都支持无参类、无参 factory 或模块内已有实例。有构造参数时写 factory 返回配置好的对象。

### Python 注入两个策略实例

```python
from dataclasses import replace
from examples.output_first import OutputLongestFirstOrder
from examples.min_tpm import MinTpmStrategy
from workload_profiling.baseline import load_config
from workload_profiling.baseline.run import execute

result, summary = execute(
    replace(load_config(), batch_size=2, batch_wait_ms=3),
    source="examples/length_requests.jsonl",
    source_format="lengths",
    output="workload_profiling/results/baseline_custom/output_first",
    batch_order=OutputLongestFirstOrder(),
    strategy=MinTpmStrategy(),
)
print(result.batches[0]["dispatch_order"])
```

`BaselineRunner(config, batch_order=..., strategy=...)` 同样支持实例注入。如果自己的类同时实现 order_batch/select，并需要共享内部状态，可以把同一实例传给这两个参数。CLI 用同一个类名加载两个接口时会分别构造实例。

验证排序效果时比较 `batches.json` 的 request_ids（到达顺序）与 dispatch_order（排序后的计划顺序）；CSV 的 batch_position 为 0-based 批内位置，events.jsonl 的 dispatched 给出实际派发顺序。被拒绝请求保留计划位置，但没有 dispatched 事件。

两个接口目前表达“批内排序 + 逐条路由”。一次联合优化所有请求的端点与执行时刻，或允许后批抢占前批，需要扩展调度计划/队列接口。

## 替换路由策略

最小契约是一个同步方法，不要求继承框架类：

```python
from workload_profiling.baseline import EndpointView, WorkloadRequest

class MyStrategy:
    def select(self, request: WorkloadRequest,
               endpoints: tuple[EndpointView, ...], now_ms: int) -> str:
        return min(endpoints, key=lambda e: e.concurrency_utilization).endpoint_id
```

引擎传入已通过 RPM/TPM/并发检查的非空候选列表。返回候选中的 endpoint_id；未知或不可用 ID 会报错。每条分配后引擎更新状态，下一次 select 拿到新快照，同批不会共享未更新的计数。视图不可修改。

### CLI 方式

随仓库的 [MinTpmStrategy](../examples/min_tpm.py) 可直接执行：

```powershell
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --strategy examples.min_tpm:MinTpmStrategy --output-dir workload_profiling/results/baseline_custom
```

自己的 `my_strategy.py` 放在仓库根或可导入的包中，使用 `--strategy my_strategy:MyStrategy`。也可把配置的 strategy 改成该规格。模块属性可以是无参策略类、无参 factory 或已实例化对象；有构造参数时用 factory：

```python
def make_strategy():
    return MyStrategy()
```

此时使用 `--strategy my_strategy:make_strategy`。同分选择规则由自定义算法负责；建议保持确定性，随机算法自行管理种子并记录参数。

### Python 方式

```python
from workload_profiling.baseline import BaselineRunner, load_config
from workload_profiling.baseline.source import read_length_requests
from examples.min_tpm import MinTpmStrategy

runner = BaselineRunner(load_config(), strategy=MinTpmStrategy())
result = runner.run(read_length_requests("examples/length_requests.jsonl"))
```

本例直接注入实例；summary 的 strategy_class 会记录真正实现类，而 strategy 字符串仍来自配置。若导出结果，记录自定义策略参数到 config/provenance，避免只看字符串误判。

## 自定义请求来源

把自己的数据适配成 `Iterable[WorkloadRequest]`：

```python
from workload_profiling.baseline import WorkloadRequest

def adapt(records):
    for index, row in enumerate(records):
        yield WorkloadRequest(
            request_id=row["id"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            source_line=index,
        )
```

tokens 必须已是非负整数，不应把缺失 usage 默默转成 0。每次 run 的 ID 唯一；到达时间仍按固定 interval 生成，原始时间戳不会自动生效。

需要计 prompt 时复用 `load_tokenizer()`、`LengthCounter.input_lengths()` 与 `output_length()`，保证与默认口径一致。不要把完整会话展开接口当作当前请求入口，详见 [数据语义](architecture.md#三种请求口径)。

## 输出预测器的接入边界

当前 baseline 提前使用录制 response，因此 output_tokens 为已知长度。WorkloadRequest 不接受未知输出。若研究发送前预测：

1. 在来源适配层计算预测输出长度或预算，作为模拟工作量输入。
2. 将真实录制长度、预测长度和预测器版本另外保存，不把预测值标成真实 usage。
3. 定义服务时间和 TPM 使用预算还是实际长度，并扩展结果字段，保持不同实验可比较。
4. 训练/评估需处理跨会话依赖和跨行重复，不用全量评估集构建预测器或基准。

框架没有内置预测器接口或在线重分类钩子；仅用不同数字构造 WorkloadRequest 可以做预算实验，但不会自动完成上述评估与结果记录。

## 真实 endpoint 与执行器

Baseline 的 EndpointConfig 只有容量和模拟速度，没有 URL、API key、model、超时或客户端字段。EndpointState.dispatch 会安排模拟完成事件，不发送 prompt。RoutingStrategy 只收到长度和状态，不能独立实现真实模型调用。

已有客户端可以通过 [RequestProcessor sender / prepare / complete](api.md#接入已有模型客户端) 接入计数。这是逐条真实请求接口，未接入 baseline 的批队列。

若要把整套调度改成真实服务，需要增加独立执行层，至少处理：

- request_id 到原始完整 prompt 的关联及各 endpoint 客户端配置；不要只发送最后一条 user。
- 真实异步提交、完成回调、失败、取消和超时，替换模拟 heap 的完成事件。
- 发送时预留预算、回包后修正实际 usage，以及失败请求如何计入 RPM/TPM。
- 同步保护在途/窗口状态，明确重试策略，避免未知发送结果造成重复调用。

这些能力当前没有实现；不要仅在 select 内发网络请求，它会阻塞虚拟时钟且破坏引擎容量与完成语义。

## 需要改代码的扩展

| 扩展 | 当前边界 / 主要位置 |
| --- | --- |
| 不规则到达或日志时间戳 | engine.py 的固定 next_arrival 生成逻辑 |
| 模型/端点兼容关系 | WorkloadRequest 没有 model 字段；需扩展模型和候选筛选 |
| 自适应 Heavy 阈值 | 目前每次 run 使用固定 config 阈值，需明确更新事件与审计记录 |
| 动态启停端点 | 当前 endpoints 为固定配置 tuple，需增加生命周期事件 |
| 跨批优先级、等待期间重排、绕过队首 | 批内排序已可替换；跨批保持释放顺序，动态重排需扩展 engine 与输出事件 |
| 重试或整批联合端点分配 | 当前逐条选择端点、无重试，需扩展计划及完成事件 |
| 超大数据流与增量导出 | source 惰性，但结果/events 在内存；需新增流式 result sink |
