# 文档索引

首次使用从 [项目首页](../README.md) 开始。以下文档各自维护一个主题，避免旧分析与新调度逻辑混在一起。

| 阅读顺序 | 文档 | 内容 |
| --- | --- | --- |
| 1 | [安装与运行](running.md) | 依赖、所有 CLI 模式、网页、测试与排错 |
| 2 | [架构与数据语义](architecture.md) | 完整上下文、时钟、批触发、端点状态、目录职责 |
| 3 | [Python API](api.md) | 对象、函数参数、返回值、异常与客户端适配 |
| 4 | [扩展指南](extensions.md) | 独立替换批内排序和路由、输入适配、输出预测和执行器边界 |
| 5 | [HTTP API](http-api.md) | 网页控制台提供的接口与完整调用流程 |
| 6 | [结果字段](results.md) | 文件、字段、事件和指标 |
| 7 | [历史离线分析](offline-profiling.md) | Stage 1 / 2 / 2.1 的数据条件与复现 |

运行示例见 [examples/README.md](../examples/README.md)。正式历史实验报告仍在 `workload_profiling/results/stage1|stage2|stage2_1/`，它们是具体实验结果，不是当前主入口的使用说明。
