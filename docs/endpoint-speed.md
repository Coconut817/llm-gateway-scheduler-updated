# Endpoint 输出速度差异实验

新配置为 workload_profiling/config/baseline_endpoint_speed.json。保持交接基线的所有条件，仅设置 A/B/C 输出生成速度为 30/20/10 tokens/ms；service_jitter_fraction 为 0。这些是人为设置的模拟参数，不是实测速度。输入处理速度与基础时延保持一致。

min_rpm 仍按可用候选的 RPM 占比选择，未增加速度评分。选定端点后，服务时间使用该端点的速度参数计算，随后影响完成时刻和并发释放时间。

## 全量对照

从项目根目录执行：

```powershell
& .\.venv\Scripts\python.exe -m examples.compare_endpoint_speed
```

脚本重新计数原始完整 prompt 数据，核对来源哈希与 tokenizer 版本，用同一组请求分别回放原速度与不同速度。它检查新配置只改变输出速度，并将原速度组 requests.csv 与 handoff_v1 逐行核对。

默认结果目录为 workload_profiling/results/reproduction/endpoint_speed_v1。comparison.md 是汇总对照，comparison.json 是完整指标与验收结果，request_differences.csv 是所有重型请求的前后差异，uniform_speed/ 和 different_speeds/ 各保存完整实验日志与配置。它不会覆盖 handoff_v1 或服务时间波动实验。再次执行需要用 --output-dir 指定新目录。

## 单独运行新场景

```powershell
& .\.venv\Scripts\python.exe baseline.py `
  --config workload_profiling/config/baseline_endpoint_speed.json `
  --output-dir workload_profiling/results/reproduction/endpoint_speed_manual_v1
```

这条命令只运行不同速度组，不自动生成两组对照报告。数据默认使用原始 prompt，全量读取；排序和路由由新配置明确设置。根 baseline.json 没有更改。

实验用于观察环境变化，尚未更换策略。A 更快而 C 更慢且容量不同，总处理能力没有保持不变，不能把延迟变化直接解释为异构性本身或算法优劣。轻型仍不消耗端点；是否存在容量等待以结果为准。
