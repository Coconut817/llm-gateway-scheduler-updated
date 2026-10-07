# Baseline 模块导航

安装与快速开始见 [项目首页](../../README.md)，完整说明统一维护在 [docs/](../../docs/README.md)。

当前交接入口：[接手运行步骤](../../docs/handoff-start.md)、[改动汇总](../../docs/handoff-guide.md)。默认仍是旧heavy_only；全请求动态功能请选baseline_dynamic_output.json。

| 文件 | 提供的能力 | 文档 |
| --- | --- | --- |
| `config.py` | `BaselineConfig`、`EndpointConfig`、`load_config()` | [Python API](../../docs/api.md) |
| `models.py` | `WorkloadRequest`、`EndpointView`、内部可变端点状态 | [Python API](../../docs/api.md)、[架构](../../docs/architecture.md) |
| `source.py` | 原始 prompt 与长度 JSONL 的惰性读取 | [Python API](../../docs/api.md) |
| `engine.py` | `BaselineRunner.run()`、`RunResult` | [Python API](../../docs/api.md) |
| `routing.py` | `RoutingStrategy`、`MinRpmStrategy`、`load_strategy()` | [策略扩展](../../docs/extensions.md) |
| `ordering.py` | FIFO、总长度、有效优先级、轻重分组排序与排列校验 | [排序接入](../../docs/extensions.md#替换批内排序) |
| `arrivals.py` | 确定性突发到达时间表 | [突发到达](../../docs/burst-arrivals.md) |
| `priority.py` | 可选业务权重、合成标签和重型折扣 | [交接汇总](../../docs/handoff-guide.md) |
| `classification.py` | 冻结ECDF与压力动态输出门槛 | [动态功能](../../docs/dynamic-output.md) |
| `run.py` | `execute()`、CLI 模块入口 | [所有运行方式](../../docs/running.md) |
| `reporting.py` | `write_outputs()` 与结果持久化 | [结果字段](../../docs/results.md) |
| `dashboard.py` | 本地控制台、后台回放与 HTTP API | [HTTP API](../../docs/http-api.md) |

从仓库根目录运行 `python baseline.py` 或 `python -m workload_profiling.baseline.run`；Python 接口导入 `workload_profiling.baseline`。默认 fifo 批内排序与 min_rpm 路由可分别用 --batch-order/--strategy 替换；自定义示例见 [output_first.py](../../examples/output_first.py) 和 [min_tpm.py](../../examples/min_tpm.py)。
