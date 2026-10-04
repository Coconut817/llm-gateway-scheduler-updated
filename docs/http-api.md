# 本地控制台 HTTP 接口

[返回首页](../README.md) · [Python API](api.md) · [运行方式](running.md)

启动：`python demo.py --port 8765`，服务绑定 `127.0.0.1`。接口供本地脚本或网页驱动一次离线回放，不是向真实 endpoint 发送 prompt 的网关。无文件上传、鉴权或多用户任务管理。

## 接口总览

| 方法 | 路径 | 成功状态 | 作用 |
| --- | --- | --- | --- |
| GET | `/` | 200 | Baseline 控制台 HTML |
| GET | `/api/baseline/config` | 200 | 磁盘默认参数字典 |
| POST | `/api/baseline/run` | 202 | 启动后台回放 |
| GET | `/api/baseline/status` | 200 | idle/running/completed/failed 与结果 |
| GET | `/api/baseline/requests?offset=0&limit=100` | 200 | 完成后的请求分页 |
| GET | `/baseline/requests.csv` | 200 | 完成后的全量 CSV 下载 |

一次服务进程只允许一个正在运行的回放；结果 API 指向当前/最近一轮结果，网页运行文件则各自保存在 `results/baseline/web/<run_id>/`。

## GET config

返回 [baseline.json](../workload_profiling/config/baseline.json) 的完整配置：批参数、两轴阈值、strategy 和 endpoints。GET 返回的是磁盘默认值，不是运行中的覆盖配置；实际生效配置保存在本轮 `config.json`。

推荐先读取默认值，在客户端修改必要字段，再把完整字典传给 POST。

## POST run

请求头 `Content-Type: application/json`，请求 body 长度应为 1 到 100,000 bytes，需可识别的 Content-Length。普通浏览器 fetch 或 urllib 会设置长度。

使用默认配置运行前 200 行：

```json
{"limit":200}
```

使用默认配置运行全部：

```json
{"limit":null}
```

覆写配置时传入 `{"config":完整配置字典,"limit":200}`。config 可省略，省略时加载默认；它不是与磁盘默认做任意字段 merge 的 PATCH，至少要提供合法 endpoints，普通省略字段使用 dataclass 默认值。建议从 GET config 取得完整值。

| 字段 | 契约 |
| --- | --- |
| `config` | 可选合法 BaselineConfig 字典；网页 API 只接受 strategy=`min_rpm` |
| `limit` | 可省略/为 null 表示全部，或正整数；0、负数、bool 无效 |

网页控件的“数量 0”会由 JavaScript 转成 null，直接调用 API 不要传 0。该 API 固定读取默认原始 prompt 文件，不接受 source/source_format/output 路径，自己的数据使用 CLI 或 execute。

提交成功立即返回 202：

```json
{"status":"running"}
```

202 表示任务已启动，不能认为请求已全部完成。配置无效、已有回放运行、无效 JSON 或 body 大小不符时返回 400：

```json
{"error":"已有回放正在运行"}
```

## GET status

### idle

```json
{"status":"idle","profiled":0}
```

### running

```json
{"status":"running","profiled":100}
```

profiled 每 100 条更新，不能当作严格实时进度；不足 100 条的一轮可一直显示 0，随后直接完成。running 状态不返回逐条中间结果或实时端点视图。

### completed

返回字典包含：

| 字段 | 内容 |
| --- | --- |
| `status` | `completed` |
| `profiled` | 本轮总请求数 |
| `summary` | 完整统计、墙钟耗时与 provenance，见 [结果字段](results.md) |
| `endpoints` | 最终端点状态列表 |
| `output_directory` | 服务机器上的本轮绝对结果目录 |

completed 表示回放已排空并导出，仍可能有 rejected 请求，检查 `summary.rejected_requests`。

### failed

```json
{"status":"failed","profiled":0,"error":"错误类别或原因"}
```

运行中源文件、tokenizer 或引擎出错，状态改为 failed。停止服务会结束后台线程，未完成任务不会跨服务重启恢复；重新启动也不会自动加载磁盘上的旧 run 到 status API。

## GET requests

| Query | 默认 | 校验 |
| --- | --- | --- |
| `offset` | 0 | 非负整数，请求列表的行偏移 |
| `limit` | 100 | 1 到 200 的整数 |

使用附带原始数据运行前 200 行时，返回示例：

```json
{"count":200,"requests":[{"request_id":"request_000000","input_tokens":615,"output_tokens":1261,"source_line":0,"total_tokens":1876,"arrival_at_ms":0,"heavy":true,"endpoint_id":"endpoint_a","status":"completed"}]}
```

上例只展示部分请求字段；实际每个元素包含完整 [CSV 对应字段](results.md#requests-csv)。count 是整轮总数，不是当前页数。超出 count 的合法 offset 返回空列表。任务未完成/参数无效返回 400，不读取之前已经被新任务替换的结果。

## GET CSV

`/baseline/requests.csv` 返回当前完成回放的整个 CSV，`Content-Type: text/csv; charset=utf-8`，下载文件名 `baseline_requests.csv`，包含 UTF-8 BOM。空单元格表示缺失。未完成返回 400。

## Python 完整调用示例

另开终端启动 `python demo.py`，再从自己的脚本执行以下代码。服务默认源文件需要存在。

```python
import json
import time
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8765"

def get_json(path):
    with urlopen(base + path, timeout=30) as response:
        return json.load(response)

config = get_json("/api/baseline/config")
config["batch_size"] = 4
config["batch_wait_ms"] = 10
body = json.dumps({"config": config, "limit": 200}).encode("utf-8")
with urlopen(Request(base + "/api/baseline/run", data=body,
                     headers={"Content-Type": "application/json"}), timeout=30) as response:
    print(response.status, json.load(response))

while True:
    state = get_json("/api/baseline/status")
    if state["status"] in {"completed", "failed"}:
        break
    time.sleep(0.5)

if state["status"] == "failed":
    raise RuntimeError(state["error"])
print(state["summary"])
print(get_json("/api/baseline/requests?offset=0&limit=100"))
with urlopen(base + "/baseline/requests.csv", timeout=30) as response:
    with open("baseline_requests.csv", "wb") as output:
        output.write(response.read())
```

该示例会在调用方工作目录生成一个 CSV；若仅需要服务器端导出文件，可省略最后的下载代码。

## 旧长度页面的接口

使用 `python demo.py --length-demo` 启动另一种服务器，同一端口不能同时启动两种模式。它依赖 Stage 2.1，而不是当前 baseline 回放结果。

| 方法与路径 | 内容 |
| --- | --- |
| GET `/` | 单条长度页面 |
| GET `/api/config` | 历史 OutputHeavyPolicy 默认百分位阈值 |
| POST `/api/profile` | 单条 prompt/可选回复长度与操作性输出标签 |
| GET `/api/dataset?page=0&size=30&heavy=0` | 历史 5,186 行完整视图，page>=0、1<=size<=100，heavy=1 过滤默认输出 Heavy |
| GET `/export.csv` | 历史完整 23 列 CSV，不是 baseline 请求轨迹 |

profile 请求示例：

```json
{"prompt":"解释二分查找","threshold":0.9,"response_mode":"typed","response":"二分查找通过折半缩小范围。"}
```

prompt 也可为完整 `{"messages":[...]}` 对象。response_mode 为 input_only/typed/example；typed 必须有字符串 response，example 使用内置演示文本。threshold 为 null/省略时恢复默认，非空时须 0<X<1。成功返回 result、active_threshold、active_threshold_source、response_mode、example_response 和 model_called=false。阈值仅对本次计算生效，不改历史标签或磁盘配置。

profile 请求大小为 1 到 2,000,000 bytes。成功 200，输入或处理错误 400，未知路径 404。这些接口与 `/api/baseline/*` 不互通。
