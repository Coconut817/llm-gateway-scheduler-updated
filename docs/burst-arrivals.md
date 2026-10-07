# 突发到达与全量对照

配置：workload_profiling/config/baseline_burst.json。保留原速度 20/20/20 tokens/ms、无耗时波动、原容量、重型阈值、输出最短优先排序与 min_rpm。只改变请求到达时间。

新增配置字段 arrival_mode（fixed/burst，默认 fixed）、burst_size（默认 512，至少 2）、burst_span_ms（默认 20，非负整数）。arrival_interval_ms 在突发模式表示标称间隔，用于确定整个观察跨度与 EOF 时刻，不表示每条请求实际间隔。

## 时间表的生成

按原数据顺序分组，每组最多 512 条。组的原始起点为组首行序号乘标称间隔，组内请求均匀挤到 20ms 跨度内，毫秒取整允许同时到达。最后不足一组时也保留原顺序。

随后把整张时间表从 0 到原始最后时刻线性缩放到固定模式的 0 到 (N-1)*arrival_interval_ms，并再次向下取整。这样保持请求数、顺序及首末到达时刻相同；实际组内跨度可能稍大于配置值。单组且配置跨度为 0 时，将最后一条保留到固定模式最后时刻。N=0 为空，N=1 在 0ms。

对于 3168 条、标称间隔 1ms 的实验，两组首末时刻均为 0ms 和 3167ms；间隔平均值均为 1ms，但局部速率不同。最后一次读取以最后到达加标称间隔作为 EOF，与固定模式一致。EOF 后继续排空所有等待和在途请求。

突发模式需要预读取全部请求，以便根据数量构建时间表；固定模式仍惰性读取。该模式用于有限数据的离线模拟，不能作为无限在线流的到达实现。

同一时刻先完成在途请求，然后消费该时刻所有到达（达到批大小即释放），再处理收集超时，最后调度。这避免逐条到达与派发穿插影响同刻容量竞争。固定模式没有同时到达，原始逐请求结果保持一致。

## 运行

完整对照（项目根目录）：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_burst_arrivals
```

结果在 workload_profiling/results/reproduction/burst_arrivals_v1/。固定组与 handoff_v1 的逐请求 CSV 完全核对，突发组重复运行以检查确定性。comparison.md、comparison.json、request_differences.csv 保存对照，arrival_schedule.csv 保存两组全部到达时刻，arrival_counts_100ms.csv 保存 [start_ms,end_ms) 桶内的请求数。fixed_arrivals/、burst_arrivals/ 保存完整配置和日志。重复对照使用 --output-dir 指定新目录。

单独跑突发场景：

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_burst.json `
  --output-dir workload_profiling/results/reproduction/burst_manual_v1
```

也可以通过 --arrival-mode burst --burst-size 512 --burst-span-ms 20 覆盖配置；排序仍需明确指定或由配置提供。

这是人为构造的突发压力，不是对真实流量分布的拟合。容量等待也包含队首阻挡；只有重型请求消耗端点，轻型立即完成。没有更换路由或排序，结果不能作为新策略提升的证据。
