# 大模型请求批调度与路由 Baseline

一个可复现的离线基线：逐条读取具有完整上下文的 prompt/response，将输入或输出长度达到阈值的请求放入重型队列，按批大小或等待超时触发调度，再路由到 RPM 利用率最低的可用模拟端点。轻型请求在到达时立即完成。

默认使用 **1ms 虚拟到达间隔**，三个模拟端点分别维护 RPM、TPM 和在途并发。路由策略可通过 Python 接口或 `module:attribute` 替换。输出长度来自数据中已记录的回答，当前基线不预测输出，不发送真实模型请求。

## 快速开始

已验证环境为 Python 3.12，建议使用同一版本。下载 ZIP 并解压，或克隆仓库后，先进入包含 `baseline.py` 的仓库根目录。

### Windows PowerShell

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 先跑随仓库提供的小型长度示例：无需原始数据、无需下载 tokenizer
& .\.venv\Scripts\python.exe -m examples.run_baseline

# 运行原始 prompt 数据；首次需要下载固定 tokenizer，不下载模型权重
& .\.venv\Scripts\python.exe baseline.py --limit 200

# 网页控制台：http://127.0.0.1:8765
& .\.venv\Scripts\python.exe demo.py --open
```

### Linux / macOS

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m examples.run_baseline
.venv/bin/python baseline.py --limit 200
.venv/bin/python demo.py --open
```

没有附带原始数据时，可以直接使用 `examples/length_requests.jsonl` 或 `examples/prompt_requests.jsonl`，也可以通过 `--source` 指定自己的文件。安装依赖需要网络；长度示例运行时不需要 tokenizer 或网络。

## 一行原始数据如何变成请求

```json
{"prompt":{"messages":[{"role":"user","content":"解释二分查找"},{"role":"assistant","content":"二分查找用于有序数组"},{"role":"user","content":"给出 Python 示例"}]} ,"response":"下面是示例……"}
```

完整 `prompt.messages` 按原角色和顺序进入 chat template，历史回答计入 input；外层 `response` 单独计入 output。**每行只产生一次请求，不展开历史回合。** 当前原始文件的 3,168 行产生 3,168 个请求；历史 Stage 1 展开出的 5,186 个样本是另一种分析口径。

```mermaid
flowchart LR
    A[逐行读取完整上下文与已记录回答] --> B[计算输入与输出 token 数]
    B --> C{输入或输出达到阈值?}
    C -- 否 --> D[到达时立即完成]
    C -- 是 --> E[收集重型请求]
    E --> F[批大小 / 超时 / 输入结束触发]
    F --> G[满足容量限制的端点]
    G --> H[可替换路由策略]
    H --> I[模拟完成与状态更新]
```

## 常用运行方式

```powershell
# 全量原始 prompt 回放
& .\.venv\Scripts\python.exe baseline.py

# 修改频率、批大小和最长收集等待时间
& .\.venv\Scripts\python.exe baseline.py --arrival-interval-ms 1 --batch-size 8 --batch-wait-ms 10

# 替换为随仓库提供的 TPM 策略，并使用长度示例
& .\.venv\Scripts\python.exe baseline.py --source examples/length_requests.jsonl --source-format lengths --strategy examples.min_tpm:MinTpmStrategy --output-dir workload_profiling/results/baseline_custom

# 所有测试
& .\.venv\Scripts\python.exe -m unittest discover -s workload_profiling/tests -t .
```

参数源为 [baseline.json](workload_profiling/config/baseline.json)。默认批大小 16、等待 20ms；输入阈值 40342.5 tokens、输出阈值 578 tokens，均可修改。容量不足时等待可超过收集超时，详见 [调度语义](docs/architecture.md)。

## 文件结构

```text
.
├── README.md                        # GitHub 首页与快速开始
├── requirements.txt                 # 依赖安装入口，引用包内固定版本
├── baseline.py                      # CLI 主入口
├── demo.py                          # 网页主入口；也保留长度演示模式
├── docs/                            # 按主题组织的完整文档
├── examples/                        # 可运行数据、策略与适配器示例
├── prompt数据/                      # 原始 prompt/response；默认 baseline 来源
├── 实验数据2/                       # 历史网关日志与容量配置；baseline 不使用
└── workload_profiling/
    ├── baseline/                    # 读取、批调度、路由、端点、CLI、HTTP 控制台
    ├── runtime/                     # 当前请求计数与同步 sender 接口
    ├── common/                      # tokenizer、消息规范化、路径、I/O、数据关联
    ├── config/                      # baseline 与历史百分位策略配置
    ├── stages/                      # 历史 Stage 1 / 2 / 2.1 分析入口
    ├── tests/                       # 行为测试与真实 artifact 集成检查
    ├── data/                        # tokenizer、长度基表、标签、reference、CSV 导出
    ├── results/                     # 历史报告与本地回放结果
    └── cache/                       # 可重建缓存与 smoke 产物
```

两份原根目录历史 CSV 已归档到 `workload_profiling/data/exports/`。默认路径相对代码所在目录解析，不依赖启动目录；用户传入的相对路径按调用方工作目录解析。详细用途与上传范围见 [架构与目录](docs/architecture.md)。

## 文档导航

| 文档 | 解决的问题 |
| --- | --- |
| [安装与运行](docs/running.md) | 环境、所有 CLI 参数、网页、长度接口、测试、常见问题 |
| [架构与数据语义](docs/architecture.md) | 完整上下文、虚拟时间、批触发、端点计数、目录职责 |
| [Python 接口](docs/api.md) | 配置、请求对象、runner、source、execute、真实客户端接入 |
| [扩展与策略接入](docs/extensions.md) | 替换路由、自定义输入、预测器与真实执行器的接入边界 |
| [HTTP 接口](docs/http-api.md) | 启动回放、轮询、分页、下载、请求与响应示例 |
| [结果字段](docs/results.md) | CSV、事件、批次、端点、summary 和指标解释 |
| [历史离线分析](docs/offline-profiling.md) | Stage 1 / 2 / 2.1 的复现、数据依赖和旧接口 |
| [可运行示例](examples/README.md) | 下载后可直接执行的示例及预期结果 |

## 运行结果与 GitHub 下载

CLI 默认写入 `workload_profiling/results/baseline/`，网页写入其中的 `web/<run_id>/`，长度示例写入 `results/baseline_example/`。每次导出参数快照、summary、请求 CSV、事件 JSONL、批次、端点状态和报告。

`.gitignore` 排除了虚拟环境、tokenizer 下载、缓存、CSV 导出及默认本地回放结果。下载者需要自行重建这些目录；代码、配置、文档和小型示例可以独立跑通长度模式。`.gitignore` 不会自动移除已经被 Git 跟踪的文件，也不会自动忽略任意自定义输出目录。

历史正式分析数据和报告继续保留，其来源与哈希记录不因文档整理而重算。当前附带数据的一次默认全量回放完成了 3,168 条请求，其中轻型 2,845、重型 323、拒绝 0；改变数据或参数后结果会变化。
