"""worker 投递循环测试：真实 HTTP（进程内 mock 供应商）覆盖全部分流路径。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config
from app.store import Store
from app.worker import Worker, deliver


def _utcnow():
    return datetime.now(timezone.utc)


class _MockVendorHandler(BaseHTTPRequestHandler):
    """路由 /ok→200 /bad→400 /error→500 /slow→睡后 200，其余 404。"""

    def _respond(self, code):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        # 记录收到的 header 供断言（投递内容正确性）
        self.server.seen_headers.append(dict(self.headers))
        if self.path == "/ok":
            self._respond(200)
        elif self.path == "/bad":
            self._respond(400)
        elif self.path == "/error":
            self._respond(500)
        elif self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(30)
            self._respond(200)
        else:
            self._respond(404)

    def log_message(self, *args):
        pass


class WorkerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vendor = ThreadingHTTPServer(("127.0.0.1", 0), _MockVendorHandler)
        cls.vendor.seen_headers = []
        cls.vendor_port = cls.vendor.server_address[1]
        threading.Thread(target=cls.vendor.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.vendor.shutdown()

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store.init(self.db_path)
        self.store.upsert_vendor("v", "V", f"http://127.0.0.1:{self.vendor_port}",
                                 "POST", "{}", "{}", [])
        self.worker = Worker(self.store)
        # 缩短轮询间隔，测试提速（不改 config 模块级值的其他消费方）
        self._orig_poll = config.POLL_INTERVAL_SECONDS
        config.POLL_INTERVAL_SECONDS = 0.05

    def tearDown(self):
        config.POLL_INTERVAL_SECONDS = self._orig_poll
        self.worker.stop_event.set()
        self.store.close()
        os.unlink(self.db_path)

    def _task(self, path="/ok", key="k"):
        task, _ = self.store.create_task(
            "v", f"http://127.0.0.1:{self.vendor_port}{path}", "POST",
            {"X-Token": "tk"}, '{"a": 1}', {"uid": "1"}, idempotency_key=key)
        return task

    # ---------- deliver() 单元 ----------

    def test_deliver_success_classified(self):
        outcome, code, err, ms = deliver(self._task("/ok"))
        self.assertEqual((outcome, code, err), ("success", 200, None))
        self.assertGreaterEqual(ms, 0)

    def test_deliver_4xx_classified(self):
        outcome, code, err, _ = deliver(self._task("/bad"))
        self.assertEqual((outcome, code), ("http_4xx", 400))

    def test_deliver_5xx_classified(self):
        outcome, code, err, _ = deliver(self._task("/error"))
        self.assertEqual((outcome, code), ("http_5xx", 500))

    def test_deliver_3xx_judged_dead_not_retry_forever(self):
        """审查发现：3xx 曾被误分类为 5xx（永动重试）。契约定不跟随重定向，
        重定向是端点配置类问题 → 判死路径，真实状态码保留。"""
        outcome, code, err, _ = deliver(self._task("/redirect"))
        self.assertEqual(outcome, "http_4xx")  # 判死桶
        self.assertEqual(code, 301)            # 真实状态码不丢
        self.assertIn("301", err)

    def test_classify_status_boundaries(self):
        from app.worker import classify_status
        self.assertEqual(classify_status(199), "http_5xx")   # 异常状态按对方故障重试
        self.assertEqual(classify_status(200), "success")
        self.assertEqual(classify_status(299), "success")
        self.assertEqual(classify_status(302), "http_4xx")
        self.assertEqual(classify_status(399), "http_4xx")
        self.assertEqual(classify_status(400), "http_4xx")
        self.assertEqual(classify_status(499), "http_4xx")
        self.assertEqual(classify_status(500), "http_5xx")
        self.assertEqual(classify_status(599), "http_5xx")

    def test_deliver_conn_error(self):
        # 未监听端口 → 连接拒绝
        task, _ = self.store.create_task("v", "http://127.0.0.1:1/x", "POST",
                                         {}, "{}", {}, idempotency_key="ce")
        outcome, code, err, _ = deliver(task)
        self.assertEqual(outcome, "conn_error")
        self.assertIsNone(code)
        self.assertTrue(err)

    def test_deliver_read_timeout(self):
        self._orig_read = config.READ_TIMEOUT_SECONDS
        config.READ_TIMEOUT_SECONDS = 0.5
        try:
            outcome, code, err, ms = deliver(self._task("/slow"))
            self.assertEqual(outcome, "timeout")
            self.assertIsNone(code)
            self.assertLess(ms, 5000)  # 未等满 30s
        finally:
            config.READ_TIMEOUT_SECONDS = self._orig_read

    def test_deliver_sends_rendered_headers_and_body(self):
        deliver(self._task("/ok"))
        # mock 服务端收到我们渲染的 header（含默认 Content-Type）
        headers = self.vendor.seen_headers[-1]
        self.assertEqual(headers.get("X-Token"), "tk")
        self.assertEqual(headers.get("Content-Type"), "application/json")

    # ---------- process() 分流 ----------

    def test_process_success_marks_done(self):
        task = self._task("/ok")
        self.worker.process(task)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["attempts"], 1)
        attempts = self.store.list_attempts(task["id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "success")

    def test_process_4xx_marks_dead_immediately(self):
        task = self._task("/bad")
        self.worker.process(task)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "dead")  # 不重试
        self.assertEqual(got["attempts"], 1)
        self.assertEqual(self.store.list_attempts(task["id"])[0]["outcome"],
                         "http_4xx")

    def test_process_5xx_retries_with_backoff(self):
        task = self._task("/error")
        before = _utcnow()
        self.worker.process(task)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "retrying")
        delay = (datetime.fromisoformat(got["next_attempt_at"]) - before
                 ).total_seconds()
        self.assertAlmostEqual(delay, config.RETRY_BASE_SECONDS, delta=2)

    def test_process_exhausts_to_dead(self):
        task = self._task("/error")
        # 连续失败到 MAX_ATTEMPTS
        for _ in range(config.MAX_ATTEMPTS):
            claimed = self.store.claim_due_task()
            if claimed is None:  # 重试未到期 → 手动放回
                self.store.conn.execute(
                    "UPDATE tasks SET next_attempt_at=? WHERE id=?",
                    (_utcnow().isoformat(), task["id"]))
                claimed = self.store.claim_due_task()
            self.worker.process(claimed)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "dead")
        self.assertEqual(got["attempts"], config.MAX_ATTEMPTS)
        # 完整历史：MAX_ATTEMPTS 条 attempt 记录（stage3 观测性决策）
        self.assertEqual(len(self.store.list_attempts(task["id"])),
                         config.MAX_ATTEMPTS)

    # ---------- run() 循环与优雅停机 ----------

    def test_run_processes_pending_then_stops(self):
        task = self._task("/ok", key="run")
        t = threading.Thread(target=self.worker.run, daemon=True)
        t.start()
        for _ in range(100):  # 最多等 10s
            if self.store.get_task(task["id"])["status"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(self.store.get_task(task["id"])["status"], "done")
        self.worker.stop_event.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())  # 优雅退出

    def test_run_stops_without_tasks(self):
        t = threading.Thread(target=self.worker.run, daemon=True)
        t.start()
        time.sleep(0.3)
        self.worker.stop_event.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())  # 空转时也能及时停

    def test_claimed_but_crashed_task_recovered_by_recover(self):
        # 崩溃恢复链路（worker 侧端到端）：claim 后进程死 → 恢复 → 重投成功
        task = self._task("/ok", key="crash")
        self.store.claim_due_task()  # 留在 delivering = 模拟崩溃
        self.assertEqual(self.store.recover_delivering(), 1)
        t = threading.Thread(target=self.worker.run, daemon=True)
        t.start()
        for _ in range(100):
            if self.store.get_task(task["id"])["status"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(self.store.get_task(task["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()
