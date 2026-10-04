# 目录与逐条接口重构验收

> 本文保留历史重构时点的验收记录。当前主入口与目录说明见 [项目首页](../../README.md)；后续整理已将 `request_lengths_full.csv`、`request_workload_stage2_full.csv` 归档到 `data/exports/`。正文中的旧路径说明不代表当前布局。

代码已分为 common 公共能力、stages/stage1|stage2|stage2_1 离线入口、runtime 逐条处理与策略、tests 集中测试。统计/绘图/报告与执行入口分离；路径、文件哈希、原子写入与 token 计数共用实现。详细目录和新命令见 [README](../README.md)。

## 数据存储与迁移

- 长度基表 `data/processed/request_lengths.parquet` SHA-256 未变，共 5,186 行。
- Stage 2 `tail_labels.parquet` 只存定位键和 4 个 tail 标签；Stage 2.1 `output_labels.parquet` 只存定位键和 5 个新增字段。
- 通过 `common.datasets.load_dataset('stage2_1')` 得到 23 列完整视图，已与迁移前 Parquet 逐列对照，值、行顺序和 null 语义一致。
- 冻结 reference 移到 `data/artifacts/`；metadata/config 的路径与标签文件 SHA-256 已更新。
- 后续阶段两份 Parquet 共从 203,388 bytes 减到 64,995 bytes。
- 旧包、重复完整 Parquet、旧 smoke 结果、失效 checkpoint 和历史临时缓存已删除。CSV 已逐值确认与正式基表/标签视图等价；当前仍存在的导出：request_lengths_full.csv、request_workload_stage2_full.csv。若仍存在，原因是外部程序占用而未强行关闭用户应用。

原始 JSONL、固定 tokenizer、科学阈值/拟合结果、正式统计与图片均保留。Stage 2 的既有算法签名仍是原始实验的 provenance，没有把目录重构声称成重新拟合。`--refresh-report` 仅从已完成的结果重建展示与标签。旧模块入口已移除，请使用 README 的新命令。

## 逐条接口

`runtime.RequestProcessor` 支持当前请求逐条 prepare/process/complete；`process_stream()` 惰性读取，在当前结果被消费后才读取下一条。sender 是调用方提供的同步发送适配器，每条仅调用一次，无批量预读、并发或自动重试。本次未配置或调用真实网络服务。

发送前仅计算 input；收到 response 后计算 output 和固定 reference 百分位/动态 Output-Heavy。尚无 response 时 output/total/标签为 null。每条输入包含完整上下文，历史 assistant 仅用于计 input，不作为新请求重复发送。错误可选择停止或逐条记录；默认不输出完整 Prompt/Response。

## 实际验证

- 68 个测试全部通过，无跳过，包括原有 48 个测试、流式契约、数据关联、失败写入保护和真实 artifact 校验。
- Stage 1 smoke：12 conversations、44 成功 requests，tokenization 失败 0。
- 真实固定 Qwen tokenizer 下，逐条接口与全部 44 条 smoke 长度/角色计数一致。
- stdin 命令行已实测：输入 hello、响应 world 时 input=13、output=1、total=14，每条输出一个 JSON 结果。
- 默认 0.95 的 Output-Heavy 仍命中 263 条，`output_tail_evt` 仍有 5186 个 null。
- Stage 1 原始 JSONL 和唯一基表 SHA-256 不变，Stage 2/2.1 完整视图与原值一致。

当前没有新增真实拥塞估计、容量模拟、hysteresis、路由或调度。此次流式接口按逐条完整请求实现，不包含模型输出 token 分片转发。
