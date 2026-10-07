# 文档索引

首次使用从 [项目首页](../README.md) 开始。以下文档各自维护一个主题，避免旧分析与新调度逻辑混在一起。

当前修改交接请先阅读 [修改记录与交接使用说明](handoff-guide.md)，其中区分旧 heavy_only 和新 all 模式，列出已实现功能、实验入口、结果目录与待完成事项。

交接阅读顺序：[接手运行步骤](handoff-start.md) → [相对原项目的改动与状态](handoff-guide.md) → [动态参数与资源复现](dynamic-output.md)。研究过程与各阶段数字保存在 [详细修改档案](change-history.md)；新同学不必先通读所有历史实验。

动态输出门槛的一条命令运行、自定义规则、输出文件和复现方法见 [动态功能使用说明](dynamic-output.md)。

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
