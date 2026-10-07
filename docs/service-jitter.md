# 服务时间波动与对照

EndpointConfig 新增两个可选字段：service_jitter_fraction（默认 0）与 service_jitter_seed（默认 20261005）。幅度必须满足 0 <= fraction < 1；种子是非负整数。旧配置无需修改。

原始未取整服务时间为 base_latency_ms + input_tokens/input_tokens_per_ms + output_tokens/output_tokens_per_ms。新时间先乘倍率，再向上取整，至少 1ms。幅度 0.2 对应约 [0.8, 1.2) 的均匀倍率；取整后整数服务时间可能略超未取整上界。它作用于整个服务时间，包括基础时延。

倍率的键是 JSON 数组 [seed, request_id, endpoint_id]，UTF-8 编码后做 SHA-256，取摘要前 8 字节的高 53 位映射到 [0,1)。不使用进程相关的 Python hash，也不依赖派发顺序或时刻。相同请求与端点组合始终取同一倍率。调度接口不提供实际倍率。

这是每次调用的模拟耗时变化，不是墙钟等待，也不是端点在某段时间内持续变慢。请求长度、路由、排序、到达方式和容量均保持原设定，轻型请求仍立即完成。

全量对照命令（项目根目录）：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_service_jitter
```

默认以 results/reproduction/handoff_v1 的实际配置和原始数据为基准，重新计数全部 prompt；验证来源哈希，进行无波动和 ±20% 波动回放。无波动 CSV 必须与原 handoff_v1 完全一致；有波动回放重复两次以验证全部结果和事件。旧结果不被覆盖。

输出为 workload_profiling/results/reproduction/service_jitter_v1：comparison.md 为对照表，comparison.json 为指标与验收结果，request_differences.csv 为逐重型请求差异，no_jitter/ 与 jitter_20pct/ 保存完整运行记录。输出目录已存在会报错；重复运行应使用 --output-dir 指定新目录。

本实验只验证环境变化及可复现性，没有替换调度策略。单个种子不代表多种子性能结论，也不保证一定产生容量等待。
