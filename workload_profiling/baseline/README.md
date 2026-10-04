# Baseline 模块导航

安装与快速开始见 [项目首页](../../README.md)，完整说明统一维护在 [docs/](../../docs/README.md)。

| 文件 | 提供的能力 | 文档 |
| --- | --- | --- |
| `config.py` | `BaselineConfig`、`EndpointConfig`、`load_config()` | [Python API](../../docs/api.md) |
| `models.py` | `WorkloadRequest`、`EndpointView`、内部可变端点状态 | [Python API](../../docs/api.md)、[架构](../../docs/architecture.md) |
| `source.py` | 原始 prompt 与长度 JSONL 的惰性读取 | [Python API](../../docs/api.md) |
| `engine.py` | `BaselineRunner.run()`、`RunResult` | [Python API](../../docs/api.md) |
| `routing.py` | `RoutingStrategy`、`MinRpmStrategy`、`load_strategy()` | [策略扩展](../../docs/extensions.md) |
| `ordering.py` | `BatchOrderStrategy`、三种内置排序、`load_batch_order()` 与排列校验 | [排序接入](../../docs/extensions.md#替换批内排序) |
| `run.py` | `execute()`、CLI 模块入口 | [所有运行方式](../../docs/running.md) |
| `reporting.py` | `write_outputs()` 与结果持久化 | [结果字段](../../docs/results.md) |
| `dashboard.py` | 本地控制台、后台回放与 HTTP API | [HTTP API](../../docs/http-api.md) |

从仓库根目录运行 `python baseline.py` 或 `python -m workload_profiling.baseline.run`；Python 接口导入 `workload_profiling.baseline`。默认 fifo 批内排序与 min_rpm 路由可分别用 --batch-order/--strategy 替换；自定义示例见 [output_first.py](../../examples/output_first.py) 和 [min_tpm.py](../../examples/min_tpm.py)。
