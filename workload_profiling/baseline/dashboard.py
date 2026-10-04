"""Local baseline controls with background replay and progress polling."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
import webbrowser

from ..common.paths import RESULTS
from .config import BaselineConfig, load_config, positive_integer
from .ordering import BUILTIN_BATCH_ORDERS
from .run import execute

PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>重型请求路由 Baseline</title><style>
:root{font:14px system-ui,"Microsoft YaHei",sans-serif;color:#193246;background:#f1f5f9}*{box-sizing:border-box}body{margin:0}main{max-width:1280px;margin:auto;padding:28px 22px}h1{font-size:30px;margin:8px 0}h2{font-size:18px;margin:0 0 16px}p{line-height:1.8;color:#61798b}.eyebrow{color:#537995;letter-spacing:2px;font-size:11px;font-weight:700}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:22px 0}.card{background:white;border:1px solid #dce5ed;border-radius:14px;padding:22px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:block;font-weight:600;margin:0 0 6px}input,textarea,select{font:inherit;width:100%;padding:10px;border:1px solid #b9cbd9;border-radius:7px;color:#193246}textarea{font:12px ui-monospace,monospace;line-height:1.65;resize:vertical}button,.button{background:#205b82;color:white;border:0;border-radius:7px;padding:11px 18px;font:inherit;cursor:pointer;text-decoration:none}button:disabled{opacity:.5;cursor:wait}.actions{display:flex;align-items:center;gap:10px;margin:18px 0 0}.secondary{background:#e8f0f6;color:#315a74}.hint{font-size:12px;margin:7px 0}.status{padding:14px;background:#edf5fa;border-radius:8px;line-height:1.7}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}.metric{padding:15px;background:#f3f7fa;border-radius:8px}.metric small{display:block;color:#688292}.metric strong{display:block;font-size:27px;margin-top:8px}.table{overflow:auto;max-height:430px;border:1px solid #e1e8ef;border-radius:8px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:11px;white-space:nowrap;text-align:left;border-bottom:1px solid #e4ecf2}th{background:#edf4f8;position:sticky;top:0}pre{max-height:240px;overflow:auto;white-space:pre-wrap;background:#f5f8fa;padding:12px;border-radius:8px;font:12px ui-monospace,monospace}.error{color:#a32f2f;margin-top:12px}.hidden{display:none}@media(max-width:850px){.grid{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr}}
</style></head><body><main><div class="eyebrow">WORKLOAD / ROUTING BASELINE</div><h1>重型请求批调度与端点路由</h1>
<p>逐条读取原始 Prompt/Response。轻型请求立即完成；输入或输出达到阈值的请求进入重型队列。按批大小或最老请求等待时间释放，先调整批内顺序，再逐条路由到 RPM 利用率最低的可用端点。</p>
<div class="grid"><section class="card"><h2>回放参数</h2><div class="fields">
<div><label for="arrival">到达间隔 · ms</label><input id="arrival" type="number" min="1"></div>
<div><label for="batch">重型批大小</label><input id="batch" type="number" min="1"></div>
<div><label for="wait">最长收集等待 · ms</label><input id="wait" type="number" min="0"></div>
<div><label for="limit">请求数量 · 0 表示全部</label><input id="limit" type="number" min="0" value="200"></div>
<div><label for="input">输入重型阈值 · tokens</label><input id="input" type="number" min="0" step="any"></div>
<div><label for="output">输出重型阈值 · tokens</label><input id="output" type="number" min="0" step="any"></div>
<div><label for="order">批内调度顺序</label><select id="order"><option value="fifo">FIFO · 到达顺序</option><option value="shortest_first">总 tokens 最短优先</option><option value="longest_first">总 tokens 最长优先</option></select></div>
</div><p class="hint">虚拟时钟，默认每 1ms 到达一条；tokenization 的实际耗时不改变到达间隔。输出长度使用数据中已有回答。容量等待可能超过收集等待时间。</p>
<div class="actions"><button id="run" disabled>运行 Baseline</button><button id="reset" class="secondary">恢复默认参数</button></div><div id="error" class="error" role="alert"></div></section>
<section class="card"><h2>端点配置</h2><label for="endpoints">RPM、TPM、并发上限及模拟服务速度</label><textarea id="endpoints" rows="13"></textarea>
<p class="hint">批释放后先按所选顺序排列，再逐条选择端点。路由使用 min_rpm：比较过去 60 秒请求数 / RPM 上限，每次分配立即更新状态。RPM、TPM、并发都参与容量检查；自定义排序和路由通过 CLI/Python 接口接入。</p></section></div>
<section class="card"><h2>回放结果</h2><div id="status" class="status" aria-live="polite">准备就绪后可以开始回放。</div>
<div class="metrics"><div class="metric"><small>总请求</small><strong id="total">—</strong></div><div class="metric"><small>轻型 / 立即完成</small><strong id="light">—</strong></div><div class="metric"><small>重型 / 路由</small><strong id="heavy">—</strong></div><div class="metric"><small>调度批次</small><strong id="batches">—</strong></div></div>
<div class="table"><table><thead><tr><th>端点</th><th>已路由</th><th>总 tokens</th><th>窗口 RPM / 上限</th><th>窗口 TPM / 上限</th><th>并发 / 上限</th><th>并发峰值</th></tr></thead><tbody id="endpointRows"></tbody></table></div>
<p id="timings"></p><div class="actions"><a id="download" class="button hidden" href="/baseline/requests.csv">下载完整请求 CSV</a></div><pre id="summary">本页使用本地模拟端点，不调用网络模型。</pre></section>
<section class="card" style="margin-top:20px"><h2>逐条请求轨迹</h2><div class="table"><table><thead><tr><th>请求</th><th>输入</th><th>输出</th><th>重型类型</th><th>到达 ms</th><th>批次 / 触发</th><th>批内位置 · 0-based</th><th>端点</th><th>调度 ms</th><th>完成 ms</th><th>排队 ms</th><th>状态</th></tr></thead><tbody id="requestRows"></tbody></table></div><div class="actions"><button id="previous" class="secondary" disabled>上一页</button><button id="next" class="secondary" disabled>下一页</button><span id="page"></span></div></section>
</main><script>
const $=id=>document.getElementById(id);let defaults=null,timer=null,offset=0,count=0;const size=100;
function reset(){if(!defaults)return;$('arrival').value=defaults.arrival_interval_ms;$('batch').value=defaults.batch_size;$('wait').value=defaults.batch_wait_ms;$('input').value=defaults.input_threshold_tokens;$('output').value=defaults.output_threshold_tokens;$('order').value=defaults.batch_order??'fifo';$('endpoints').value=JSON.stringify(defaults.endpoints,null,2);}
function row(target,values){const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=value===null?'—':String(value);tr.append(td);}target.append(tr);}
async function requestPage(){try{const r=await fetch('/api/baseline/requests?offset='+offset+'&limit='+size);const d=await r.json();if(!r.ok)throw Error(d.error);count=d.count;$('requestRows').replaceChildren();for(const x of d.requests)row($('requestRows'),[x.request_id,x.input_tokens,x.output_tokens,x.input_heavy&&x.output_heavy?'输入 + 输出':x.input_heavy?'输入':x.output_heavy?'输出':'轻型',x.arrival_at_ms,x.batch_id===null?'—':x.batch_id+' / '+x.batch_trigger,x.batch_position,x.endpoint_id,x.dispatch_at_ms,x.finished_at_ms,x.queue_wait_ms,x.status]);$('previous').disabled=offset===0;$('next').disabled=offset+size>=count;$('page').textContent='第 '+(Math.floor(offset/size)+1)+' / '+Math.max(1,Math.ceil(count/size))+' 页 · '+count+' 条';}catch(e){$('error').textContent=e.message;}}
async function poll(){try{const r=await fetch('/api/baseline/status');const d=await r.json();if(!r.ok)throw Error(d.error);if(d.status==='running'){$('status').textContent='正在读取与计数：已处理 '+d.profiled+' 条。运行期间可继续查看进度。';return;}clearInterval(timer);timer=null;$('run').disabled=false;$('reset').disabled=false;if(d.status==='failed')throw Error(d.error);if(d.status!=='completed')return;const s=d.summary;$('status').textContent='回放完成：'+s.completed_requests+' 条完成，'+s.rejected_requests+' 条拒绝。结果目录：'+d.output_directory;for(const [id,key]of [['total','total_requests'],['light','light_requests'],['heavy','heavy_requests'],['batches','batch_count']])$(id).textContent=s[key].toLocaleString();$('endpointRows').replaceChildren();for(const e of d.endpoints)row($('endpointRows'),[e.endpoint_id,e.total_requests,e.total_tokens,e.requests_in_window+' / '+e.rpm_limit,e.tokens_in_window+' / '+e.tpm_limit,e.concurrency+' / '+e.concurrency_limit,e.peak_concurrency]);$('timings').textContent='模拟结束：'+s.simulation_end_ms+' ms · 重型平均排队：'+s.heavy_queue_wait_ms.mean.toFixed(2)+' ms · 重型 P95 完成延迟：'+s.heavy_latency_ms.p95+' ms';$('summary').textContent=JSON.stringify(s,null,2);$('download').classList.remove('hidden');offset=0;await requestPage();}catch(e){clearInterval(timer);timer=null;$('run').disabled=false;$('reset').disabled=false;$('error').textContent=e.message;$('status').textContent='回放未完成，请检查参数。';}}
$('run').onclick=async()=>{$('error').textContent='';try{const config={...defaults,arrival_interval_ms:Number($('arrival').value),batch_size:Number($('batch').value),batch_wait_ms:Number($('wait').value),input_threshold_tokens:Number($('input').value),output_threshold_tokens:Number($('output').value),batch_order:$('order').value,endpoints:JSON.parse($('endpoints').value)};const limit=Number($('limit').value);const r=await fetch('/api/baseline/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config,limit:limit===0?null:limit})});const d=await r.json();if(!r.ok)throw Error(d.error);$('run').disabled=true;$('reset').disabled=true;$('download').classList.add('hidden');$('requestRows').replaceChildren();$('endpointRows').replaceChildren();$('status').textContent='正在启动回放…';timer=setInterval(poll,700);await poll();}catch(e){$('error').textContent=e.message;}};
$('reset').onclick=reset;$('previous').onclick=()=>{offset=Math.max(0,offset-size);requestPage();};$('next').onclick=()=>{offset+=size;requestPage();};fetch('/api/baseline/config').then(r=>r.json()).then(d=>{defaults=d;reset();$('run').disabled=false;}).catch(e=>{$('error').textContent=e.message;});
</script></body></html>'''


class DashboardService:
    def __init__(self, *, tokenizer=None, tokenizer_metadata=None):
        self.tokenizer = tokenizer
        self.tokenizer_metadata = tokenizer_metadata
        self.lock = threading.Lock()
        self.state = {"status": "idle", "profiled": 0}
        self.result = None
        self.output = None

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def start(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("请求必须为 JSON 对象")
        config = BaselineConfig.from_dict(payload.get("config", load_config().to_dict()))
        # UI exposes the built-in baseline. Custom Python strategies use CLI/config.
        if config.strategy != "min_rpm":
            raise ValueError("网页控制台目前使用 min_rpm；自定义策略请使用命令行")
        if config.batch_order not in BUILTIN_BATCH_ORDERS:
            raise ValueError("网页只支持内置批内排序；自定义排序请使用命令行或 Python 接口")
        limit = payload.get("limit")
        if limit is not None:
            positive_integer(limit, "limit")
        with self.lock:
            if self.state["status"] == "running":
                raise ValueError("已有回放正在运行")
            self.result = None
            self.output = RESULTS / "baseline/web" / uuid4().hex
            self.state = {"status": "running", "profiled": 0}

        def progress(n):
            with self.lock:
                self.state["profiled"] = n

        def work():
            try:
                if self.tokenizer is None:
                    from ..common.tokenizer import load_tokenizer
                    self.tokenizer, self.tokenizer_metadata = load_tokenizer()
                result, summary = execute(config, limit=limit, output=self.output,
                                          tokenizer=self.tokenizer, tokenizer_metadata=self.tokenizer_metadata,
                                          progress=progress)
                with self.lock:
                    self.result = result
                    self.state = {"status": "completed", "profiled": summary["total_requests"],
                                  "summary": summary, "endpoints": result.endpoints,
                                  "output_directory": str(self.output)}
            except Exception as error:
                with self.lock:
                    self.state = {"status": "failed", "profiled": self.state["profiled"],
                                  "error": str(error) if isinstance(error, (ValueError, OSError)) else type(error).__name__}

        threading.Thread(target=work, daemon=True).start()
        return {"status": "running"}

    def request_page(self, offset, limit):
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("非法分页参数")
        with self.lock:
            if self.state["status"] != "completed" or self.result is None:
                raise ValueError("回放尚未完成")
            return {"count": len(self.result.requests), "requests": deepcopy(self.result.requests[offset:offset+limit])}

    def csv(self):
        with self.lock:
            if self.state["status"] != "completed":
                raise ValueError("回放尚未完成")
            return (self.output / "requests.csv").read_bytes()


def create_server(service, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8", download=False):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8") if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="baseline_requests.csv"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif parsed.path == "/api/baseline/config":
                    self.send(200, load_config().to_dict())
                elif parsed.path == "/api/baseline/status":
                    self.send(200, service.snapshot())
                elif parsed.path == "/api/baseline/requests":
                    query = parse_qs(parsed.query)
                    self.send(200, service.request_page(int(query.get("offset", [0])[0]), int(query.get("limit", [100])[0])))
                elif parsed.path == "/baseline/requests.csv":
                    self.send(200, service.csv(), "text/csv; charset=utf-8", download=True)
                else:
                    self.send(404, {"error": "未找到页面"})
            except (ValueError, TypeError, OSError):
                self.send(400, {"error": "请求参数无效，或回放尚未完成"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/baseline/run":
                self.send(404, {"error": "未知接口"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 100000:
                    raise ValueError("请求大小应在 1 byte 到 100 KB 之间")
                payload = json.loads(self.rfile.read(length))
                self.send(202, service.start(payload))
            except (ValueError, TypeError, KeyError) as error:
                self.send(400, {"error": str(error)})

    return HTTPServer(("127.0.0.1", port), Handler)


def serve(port=8765, *, open_browser=False):
    service = DashboardService()
    server = create_server(service, port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Baseline dashboard: {url}\nCtrl+C to stop", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
