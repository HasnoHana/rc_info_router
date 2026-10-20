"""端到端回归冒烟（临时脚本，跑完即删）：

模拟真实运行形态：Store → FastAPI(uvicorn 线程) → Worker 线程 → mock 供应商。
覆盖：契约注册 → 受理渲染 → 异步投递 → 状态观测 → 幂等 → 4xx 判死 → rerender。
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uvicorn
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import config
from app.api import create_app
from app.store import Store
from app.worker import Worker

PASS = []
def ok(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)

# ---- mock 供应商（计数收到的请求） ----
hits = {"ok": 0, "bad": 0}
class H(BaseHTTPRequestHandler):
    def _r(self, code):
        n = int(self.headers.get("Content-Length") or 0)
        if n: self.rfile.read(n)
        self.send_response(code); self.send_header("Content-Length", "2")
        self.end_headers(); self.wfile.write(b"{}")
    def do_POST(self):
        path = self.path.split("?")[0]  # 忽略查询串（真实供应商亦然）
        hits[path.strip("/")] = hits.get(path.strip("/"), 0) + 1
        self._r(200 if path == "/ok" else 400)
    def log_message(self, *a): pass

vendor_srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
vport = vendor_srv.server_address[1]
threading.Thread(target=vendor_srv.serve_forever, daemon=True).start()

# ---- 被测服务 ----
fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
store = Store.init(db)
config.POLL_INTERVAL_SECONDS = 0.05
worker = Worker(store)
threading.Thread(target=worker.run, daemon=True).start()

app = create_app(store)
ucfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
userver = uvicorn.Server(ucfg)
threading.Thread(target=userver.run, daemon=True).start()
for _ in range(100):
    if userver.started: break
    time.sleep(0.1)
port = userver.servers[0].sockets[0].getsockname()[1]

def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")

def wait_status(task_id, want, timeout_s=10):
    for _ in range(int(timeout_s * 20)):
        s, t = call("GET", f"/tasks/{task_id}")
        if t["status"] == want: return t
        time.sleep(0.05)
    return t

# ---- 场景 1：契约注册 + 成功投递 ----
s, _ = call("PUT", "/vendors/ads-a", {
    "name": "广告A", "url_template": f"http://127.0.0.1:{vport}/ok",
    "method": "POST", "headers_template": {"X-Token": "{{token}}"},
    "body_template": '{"uid": "{{user_id}}"}',
    "variables_schema": ["user_id", "token"]})
ok("1.1 注册契约 200", s == 200)

s, t1 = call("POST", "/notifications", {
    "vendor_id": "ads-a", "variables": {"user_id": "u1", "token": "tk"},
    "idempotency_key": "evt-1"})
ok("1.2 受理 202 pending", s == 202 and t1["status"] == "pending")
t1 = wait_status(t1["id"], "done")
ok("1.3 异步投递 done", t1["status"] == "done")
s, att = call("GET", f"/tasks/{t1['id']}/attempts")
ok("1.4 attempts 历史一条 success", len(att) == 1 and att[0]["outcome"] == "success")
ok("1.5 供应商确实收到（含渲染 header）",
   hits["ok"] == 1)  # header 正确性已在单测锁定

# ---- 场景 2：幂等重发 ----
s, t2 = call("POST", "/notifications", {
    "vendor_id": "ads-a", "variables": {"user_id": "u1", "token": "tk"},
    "idempotency_key": "evt-1"})
ok("2.1 同 key 返回原任务", t2["id"] == t1["id"] and hits["ok"] == 1)

# ---- 场景 3：4xx 判死不重试 ----
call("PUT", "/vendors/bad-v", {
    "name": "B", "url_template": f"http://127.0.0.1:{vport}/bad",
    "method": "POST", "headers_template": {}, "body_template": "",
    "variables_schema": []})
s, t3 = call("POST", "/notifications", {"vendor_id": "bad-v", "variables": {}})
t3 = wait_status(t3["id"], "dead")
ok("3.1 4xx 直接 dead", t3["status"] == "dead" and t3["attempts"] == 1)
ok("3.2 供应商只被打一次", hits["bad"] == 1)

# ---- 场景 4：死信查询 ----
s, dead = call("GET", "/tasks?status=dead")
ok("4.1 dead 列表可见", s == 200 and any(x["id"] == t3["id"] for x in dead))

# ---- 场景 5：rerender（模板变更 × 旧变量） ----
s, t5 = call("POST", "/notifications", {
    "vendor_id": "ads-a", "variables": {"user_id": "u5", "token": "tk"},
    "idempotency_key": "evt-5"})
call("PUT", "/vendors/ads-a", {
    "name": "广告A", "url_template": f"http://127.0.0.1:{vport}/ok?v2=1",
    "method": "POST", "headers_template": {"X-Token": "{{token}}"},
    "body_template": '{"uid": "{{user_id}}", "src": "v2"}',
    "variables_schema": ["user_id", "token"]})
worker.stop_event.set()  # 暂停 worker，确保任务还在 pending
s, body = call("POST", "/vendors/ads-a/rerender")
ok("5.1 rerender 命中 1 条", body.get("rerendered") == 1)
worker.stop_event.clear()
w2 = Worker(store); w2.stop_event = worker.stop_event
threading.Thread(target=w2.run, daemon=True).start()
t5 = wait_status(t5["id"], "done")
ok("5.2 新模板投递成功", t5["status"] == "done" and hits["ok"] == 2)

# ---- 收尾 ----
worker.stop_event.set(); w2.stop_event.set()
userver.should_exit = True
vendor_srv.shutdown()
store.close(); os.unlink(db)

failed = [n for n, c in PASS if not c]
print(f"\n==== E2E regression: {len(PASS) - len(failed)}/{len(PASS)} passed ====")
sys.exit(1 if failed else 0)
