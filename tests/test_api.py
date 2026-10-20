"""API 层测试：真实 uvicorn 线程 + 标准库 urllib（零新增依赖）。

覆盖契约 openapi.yaml 的 8 个端点中的核心路径与错误路径。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uvicorn

from app.api import create_app
from app.store import Store


class _Server:
    """在随机端口起一个真实 uvicorn 服务。"""

    def __init__(self, store: Store):
        app = create_app(store)
        config = uvicorn.Config(app, host="127.0.0.1", port=0,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(50):  # 最多等 5s
            if self.server.started:
                break
            time.sleep(0.1)
        if not self.server.started:
            raise RuntimeError("uvicorn failed to start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store.init(self.db_path)
        self.store.upsert_vendor(
            "ads-a", "广告A", "https://ads.example/cb?uid={{user_id}}",
            "POST", {"X-Token": "{{token}}"}, '{"uid": "{{user_id}}"}',
            ["user_id", "token"])
        self.srv = _Server(self.store)

    def tearDown(self):
        self.srv.stop()
        self.store.close()
        os.unlink(self.db_path)

    # ---------- 受理 ----------

    def test_submit_202_and_task_created(self):
        status, body = self.srv.request("POST", "/notifications", {
            "vendor_id": "ads-a",
            "variables": {"user_id": "u1", "token": "tk"}})
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "pending")
        # 快照已渲染（ADR-002 全链路：API → 渲染 → 落库）
        task = self.store.get_task(body["id"])
        self.assertEqual(task["url"], "https://ads.example/cb?uid=u1")
        self.assertEqual(json.loads(task["body"]), {"uid": "u1"})

    def test_submit_unknown_vendor_400(self):
        status, _ = self.srv.request("POST", "/notifications", {
            "vendor_id": "ghost", "variables": {}})
        self.assertEqual(status, 400)

    def test_submit_missing_variable_400(self):
        status, body = self.srv.request("POST", "/notifications", {
            "vendor_id": "ads-a", "variables": {"user_id": "u1"}})
        self.assertEqual(status, 400)
        self.assertIn("missing", body["detail"])

    def test_submit_idempotency_hit_returns_same_task(self):
        payload = {"vendor_id": "ads-a",
                   "variables": {"user_id": "u1", "token": "tk"},
                   "idempotency_key": "evt-1"}
        s1, b1 = self.srv.request("POST", "/notifications", payload)
        s2, b2 = self.srv.request("POST", "/notifications", payload)
        self.assertEqual([s1, s2], [202, 202])
        self.assertEqual(b1["id"], b2["id"])
        # 库里只有一条
        self.assertEqual(len(self.store.list_tasks()), 1)

    # ---------- vendors ----------

    def test_vendor_put_get_list(self):
        status, _ = self.srv.request("PUT", "/vendors/crm-1", {
            "name": "CRM", "url_template": "https://crm.example/{{cid}}",
            "method": "PUT", "headers_template": {},
            "body_template": "", "variables_schema": ["cid"]})
        self.assertEqual(status, 200)
        status, v = self.srv.request("GET", "/vendors/crm-1")
        self.assertEqual(status, 200)
        self.assertEqual(v["method"], "PUT")
        status, lst = self.srv.request("GET", "/vendors")
        self.assertEqual(status, 200)
        self.assertEqual({x["vendor_id"] for x in lst}, {"ads-a", "crm-1"})

    def test_vendor_put_invalid_method_400(self):
        status, _ = self.srv.request("PUT", "/vendors/bad", {
            "name": "x", "url_template": "https://x.example",
            "method": "DELETE"})  # 契约只允许 POST/PUT/PATCH
        self.assertEqual(status, 400)

    def test_vendor_get_404(self):
        status, _ = self.srv.request("GET", "/vendors/nope")
        self.assertEqual(status, 404)

    # ---------- rerender ----------

    def test_rerender_endpoint_updates_pending(self):
        _, task = self.srv.request("POST", "/notifications", {
            "vendor_id": "ads-a",
            "variables": {"user_id": "u9", "token": "tk"}})
        # 契约升级 v2
        self.srv.request("PUT", "/vendors/ads-a", {
            "name": "广告A", "url_template":
                "https://ads.example/v2/cb?uid={{user_id}}",
            "method": "POST", "headers_template": {},
            "body_template": '{"uid": "{{user_id}}"}',
            "variables_schema": ["user_id", "token"]})
        status, body = self.srv.request("POST", "/vendors/ads-a/rerender")
        self.assertEqual(status, 200)
        self.assertEqual(body["rerendered"], 1)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["url"], "https://ads.example/v2/cb?uid=u9")

    def test_rerender_unknown_vendor_404(self):
        status, _ = self.srv.request("POST", "/vendors/nope/rerender")
        self.assertEqual(status, 404)

    # ---------- tasks 查询 ----------

    def test_get_task_404(self):
        status, _ = self.srv.request("GET", "/tasks/no-such-id")
        self.assertEqual(status, 404)

    def test_list_tasks_by_status(self):
        self.srv.request("POST", "/notifications", {
            "vendor_id": "ads-a",
            "variables": {"user_id": "u1", "token": "tk"}})
        status, lst = self.srv.request("GET", "/tasks?status=pending")
        self.assertEqual(status, 200)
        self.assertEqual(len(lst), 1)
        status, lst = self.srv.request("GET", "/tasks?status=dead")
        self.assertEqual(lst, [])
        status, _ = self.srv.request("GET", "/tasks?status=bogus")
        self.assertEqual(status, 400)

    def test_attempts_of_task(self):
        _, task = self.srv.request("POST", "/notifications", {
            "vendor_id": "ads-a",
            "variables": {"user_id": "u1", "token": "tk"}})
        status, attempts = self.srv.request(
            "GET", f"/tasks/{task['id']}/attempts")
        self.assertEqual(status, 200)
        self.assertEqual(attempts, [])  # 尚未投递

    def test_attempts_404_for_missing_task(self):
        status, _ = self.srv.request("GET", "/tasks/ghost/attempts")
        self.assertEqual(status, 404)


    # ---------- 审查补充的边界用例 ----------

    def test_put_vendor_placeholder_not_in_schema_400(self):
        status, body = self.srv.request("PUT", "/vendors/v-bad", {
            "name": "x", "url_template": "https://x.example/{{undeclared}}",
            "method": "POST", "headers_template": {},
            "body_template": "", "variables_schema": []})
        self.assertEqual(status, 400)
        self.assertIn("undeclared", body["detail"])

    def test_put_vendor_non_http_url_400(self):
        status, _ = self.srv.request("PUT", "/vendors/v-bad2", {
            "name": "x", "url_template": "ftp://files.example/{{a}}",
            "method": "POST", "headers_template": {},
            "body_template": "", "variables_schema": ["a"]})
        self.assertEqual(status, 400)

    def test_submit_extra_variable_400(self):
        status, body = self.srv.request("POST", "/notifications", {
            "vendor_id": "ads-a",
            "variables": {"user_id": "u", "token": "t", "junk": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("unexpected", body["detail"])

    def test_negative_limit_400_not_unbounded(self):
        # 审查发现：SQLite LIMIT -1 = 全表 → 负数必须被显式拒绝（400），
        # 不能落到 SQL 层变成无界查询
        status, body = self.srv.request("GET", "/tasks?limit=-5")
        self.assertEqual(status, 400)
        self.assertIn("limit", body["detail"])

    def test_limit_over_maximum_capped(self):
        status, lst = self.srv.request("GET", "/tasks?limit=99999")
        self.assertEqual(status, 200)  # 静默钳到 200，不报错

    def test_rerender_with_no_pending_returns_zero(self):
        status, body = self.srv.request("POST", "/vendors/ads-a/rerender")
        self.assertEqual(status, 200)
        self.assertEqual(body["rerendered"], 0)

    def test_put_vendor_overwrites_existing(self):
        # 二次 PUT 更新模板后，新提交按新模板渲染
        self.srv.request("PUT", "/vendors/ads-a", {
            "name": "广告A", "url_template": "https://ads.example/v2/{{user_id}}",
            "method": "POST", "headers_template": {},
            "body_template": '{"uid": "{{user_id}}"}',
            "variables_schema": ["user_id", "token"]})
        _, task = self.srv.request("POST", "/notifications", {
            "vendor_id": "ads-a",
            "variables": {"user_id": "z1", "token": "t"}})
        got = self.store.get_task(task["id"])
        self.assertEqual(got["url"], "https://ads.example/v2/z1")


if __name__ == "__main__":
    unittest.main()
