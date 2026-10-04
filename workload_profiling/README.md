# workload_profiling 包导航

项目主页、安装和快速开始见 [根 README](../README.md)。详细文档统一放在仓库根目录的 `docs/`，此处提供代码导航。

| 模块 | 职责 | 文档 |
| --- | --- | --- |
| `baseline/` | 原始请求回放、重型批调度、模拟端点与可替换路由 | [架构](../docs/architecture.md)、[Python API](../docs/api.md) |
| `runtime/` | 当前请求计数、同步发送适配器、历史百分位策略 | [Python API](../docs/api.md)、[运行方式](../docs/running.md) |
| `common/` | 消息规范化、Qwen 计数、路径、文件校验、数据关联 | [数据语义](../docs/architecture.md) |
| `config/` | baseline 与独立的历史百分位策略配置 | [配置参数](../docs/running.md#配置文件) |
| `stages/` | Stage 1 / 2 / 2.1 的历史离线实验 | [离线分析复现](../docs/offline-profiling.md) |
| `tests/` | 单元测试与现有缓存/产物集成验证 | [测试运行](../docs/running.md#验证项目) |
| `data/`、`results/`、`cache/` | 派生表、报告、运行产物与临时缓存 | [目录说明](../docs/architecture.md#目录职责)、[结果字段](../docs/results.md) |

新 baseline 使用 token 长度阈值与模拟容量；旧 `OutputHeavyPolicy` 使用冻结 ECDF 和外部拥塞状态。两者没有自动联动，不能混用阈值单位。新 baseline 每原始行只产生一次请求，旧 Stage 1 会展开历史 assistant。
