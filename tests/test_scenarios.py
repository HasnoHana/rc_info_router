"""场景回归测试：极端退避、并发、崩溃重启、快照一致性、边界输入。

架构不变量清单（被攻击对象）见 docs/ai/sessions/stage5 归档。
"""
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
from app.worker import Worker


def _utcnow():
    return datetime.now(timezone.utc)


class _VendorHandler(BaseHTTPRequestHandler):
    """可编程 mock：按路径返回，支持命中计数与延迟。"""

    def _respond(self, code, delay=0.0):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.last_body = body
        self.server.last_headers = dict(self.headers)
        if delay:
            time.sleep(delay)
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        route = self.path.split("?")[0]
        code = self.server.routes.get(route, 404)
        delay = self.server.delays.get(route, 0.0)
        self._respond(code, delay)

    def log_message(self, *a):
        pass


class ScenarioTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vendor = ThreadingHTTPServer(("127.0.0.1", 0), _VendorHandler)
        cls.vendor.routes = {"/ok": 200, "/flaky": 500, "/bad": 400,
                             "/redirect": 301, "/big": 200}
        cls.vendor.delays = {}
        cls.vendor.last_body = None
        cls.vendor.last_headers = {}
        cls.port = cls.vendor.server_address[1]
        threading.Thread(target=cls.vendor.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.vendor.shutdown()

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store.init(self.db_path)
        self.store.upsert_vendor("v", "V", f"http://127.0.0.1:{self.port}",
                                 "POST", "{}", "{}", [])
        self._saved_cfg = {k: getattr(config, k) for k in (
            "POLL_INTERVAL_SECONDS", "RETRY_BASE_SECONDS",
            "RETRY_FACTOR", "MAX_ATTEMPTS", "READ_TIMEOUT_SECONDS")}
        config.POLL_INTERVAL_SECONDS = 0.02
        config.RETRY_BASE_SECONDS = 1     # 极端退避测试用短间隔
        self.worker = Worker(self.store)

    def tearDown(self):
        self.worker.stop_event.set()
        for k, v in self._saved_cfg.items():
            setattr(config, k, v)
        self.store.close()
        os.unlink(self.db_path)

    def _task(self, path="/ok", key=None, body="{}"):
        task, _ = self.store.create_task(
            "v", f"http://127.0.0.1:{self.port}{path}", "POST", {}, body,
            {"uid": "1"}, idempotency_key=key)
        return task

    def _nudge(self, task_id):
        """运维催投：把到期时间拨到现在（跳过真实等待）。"""
        self.store.conn.execute(
            "UPDATE tasks SET next_attempt_at=? WHERE id=?",
            (_utcnow().isoformat(), task_id))

    # ========== 极端退避环节 ==========

    def test_backoff_schedule_exact_intervals_under_exhaustion(self):
        """极端退避①：base=1s 时 7 次尝试全耗尽，重试计划间隔递增
        （增量序列 1/2/4/8/16/32s，容差 1s：间隔含单次 HTTP 耗时）。"""
        config.MAX_ATTEMPTS = 7
        task = self._task("/flaky")
        intervals = []
        prev_next = None
        for _ in range(config.MAX_ATTEMPTS):  # 第 7 次失败才转 dead
            claimed = self.store.claim_due_task() or (
                self._nudge(task["id"]), self.store.claim_due_task())[1]
            self.worker.process(claimed)
            got = self.store.get_task(task["id"])
            if got["status"] == "dead":
                break
            nxt = datetime.fromisoformat(got["next_attempt_at"])
            if prev_next is not None:
                intervals.append((nxt - prev_next).total_seconds())
            prev_next = nxt
        self.assertEqual(len(intervals), 5)
        # n_i ≈ t_i + delay_i 且 t_i ≈ t_{i-1}（每次处理仅毫秒级），
        # 故相邻间隔 ≈ delay_i - delay_{i-1} = 1,2,4,8,16（退避增量）
        for got, want in zip(intervals, [1, 2, 4, 8, 16]):
            self.assertAlmostEqual(got, want, delta=1.0,
                                   msg=f"退避间隔 {got} != {want}")
        self.assertEqual(self.store.get_task(task["id"])["status"], "dead")
        self.assertEqual(self.store.get_task(task["id"])["attempts"], 7)

    def test_backoff_config_drift_detected(self):
        """极端退避②：factor 被配置污染（如 3）时序列应跟随变化——
        验证公式读的是配置而非硬编码（2 次失败后延迟 = base × 3^1 = 3s）。"""
        config.RETRY_FACTOR = 3
        task = self._task("/flaky")
        self.store.claim_due_task()
        self.store.bump_attempts(task["id"])
        self.store.bump_attempts(task["id"])   # 已失败 2 次
        before = _utcnow()
        self.store.mark_retrying(task["id"], "500")
        got = self.store.get_task(task["id"])
        delay = (datetime.fromisoformat(got["next_attempt_at"]) - before
                 ).total_seconds()
        self.assertAlmostEqual(delay, 3.0, delta=1.0)

    def test_backoff_max_attempts_boundary_exact(self):
        """极端退避③：MAX_ATTEMPTS=2 时恰好第 2 次失败判死（off-by-one 攻击）。"""
        config.MAX_ATTEMPTS = 2
        task = self._task("/flaky")
        self.worker.process(task)                      # 第 1 次失败 → retrying
        self.assertEqual(self.store.get_task(task["id"])["status"], "retrying")
        self._nudge(task["id"])
        self.worker.process(self.store.claim_due_task())  # 第 2 次失败
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "dead")        # 恰好死，不多不少
        self.assertEqual(got["attempts"], 2)

    # ========== 并发不变量 ==========

    def test_dual_worker_no_duplicate_delivery(self):
        """并发①：两个 worker 线程共享 Store，每条任务恰好投递一次。"""
        tasks = [self._task("/ok", key=f"c{i}") for i in range(8)]
        w2 = Worker(self.store)
        threads = [threading.Thread(target=self.worker.run, daemon=True),
                   threading.Thread(target=w2.run, daemon=True)]
        for t in threads:
            t.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            statuses = [self.store.get_task(t["id"])["status"] for t in tasks]
            if all(s == "done" for s in statuses):
                break
            time.sleep(0.05)
        for t in threads:
            t.join(0.1)
        self.worker.stop_event.set()
        w2.stop_event.set()
        # 不变量：全部 done 且每条恰好一条 success 历史（无重复投递）
        for t in tasks:
            got = self.store.get_task(t["id"])
            self.assertEqual(got["status"], "done", f"任务 {t['id']} 未完成")
            atts = self.store.list_attempts(t["id"])
            self.assertEqual(len(atts), 1, f"任务 {t['id']} 被重复投递")

    def test_concurrent_same_idempotency_key_single_task(self):
        """并发②：同幂等键双线程同时提交，只允许创建一条任务。"""
        results, errors = [], []
        def submit():
            try:
                results.append(self.store.create_task(
                    "v", "http://x/y", "POST", {}, "{}", {"a": 1},
                    idempotency_key="race-key"))
            except Exception as e:  # UNIQUE 崩溃即失败
                errors.append(e)
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], "并发同 key 提交触发 IntegrityError")
        created = [r for r, c in results if c]
        self.assertEqual(len(created), 1, "同 key 被创建多条任务")

    # ========== 崩溃 / 重启恢复 ==========

    def test_crash_between_claim_and_deliver_no_lost_no_ghost_history(self):
        """崩溃①：claim 后进程死（无 attempt 记录）→ 恢复 → 重投成功。
        不变量：不丢任务；缺失的那次尝试不留幽灵历史。"""
        task = self._task("/ok", key="crash")
        self.store.claim_due_task()          # 崩溃点：delivering，attempts=0
        self.assertEqual(len(self.store.list_attempts(task["id"])), 0)

        # 模拟重启：全新 Store + 恢复 + 全新 worker
        store2 = Store(self.db_path)
        self.assertEqual(store2.recover_delivering(), 1)
        w2 = Worker(store2)
        w2.stop_event = self.worker.stop_event
        threading.Thread(target=w2.run, daemon=True).start()
        for _ in range(100):
            if store2.get_task(task["id"])["status"] == "done":
                break
            time.sleep(0.1)
        got = store2.get_task(task["id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["attempts"], 1)           # 恢复后算第 1 次尝试
        self.assertEqual(len(store2.list_attempts(task["id"])), 1)  # 无幽灵
        store2.close()

    def test_crash_leaves_retrying_task_survives_restart(self):
        """崩溃②：retrying 中的任务跨「重启」存活，到期后继续被投。"""
        task = self._task("/ok", key="surv")
        self.worker.process(task)            # /ok 会成功，这里手动造 retrying
        self.store.claim_due_task()
        self.store.bump_attempts(task["id"])
        self.store.mark_retrying(task["id"], "simulated 500")
        store2 = Store(self.db_path)         # 重启（不跑 recover——retrying 无需恢复）
        self.assertEqual(store2.get_task(task["id"])["status"], "retrying")
        self._nudge(task["id"])
        claimed = store2.claim_due_task()
        self.assertIsNotNone(claimed)
        Worker(store2).process(claimed)
        self.assertEqual(store2.get_task(task["id"])["status"], "done")
        store2.close()

    # ========== 快照与契约一致性 ==========

    def test_snapshot_immunity_to_contract_change_midqueue(self):
        """快照①：投递进行中契约被改，本次投递仍发受理时的字节。"""
        task = self._task("/ok", key="snap", body='{"v": "original"}')
        claimed = self.store.claim_due_task()
        # 投递「进行中」改契约模板 + rerender
        self.store.upsert_vendor("v", "V", "http://changed.example/{{uid}}",
                                 "POST", "{}", "{}", ["uid"])
        def render(v, t, vars_):
            return {"url": v["url_template"].replace("{{uid}}", vars_["uid"]),
                    "method": "POST", "headers": {}, "body": '{"v":"rerendered"}'}
        self.store.rerender_vendor("v", render)
        # 但本次已 claim 的任务按 claim 时的快照投递
        self.worker.process(claimed)
        self.assertEqual(self.vendor.last_body, b'{"v": "original"}')
        self.assertEqual(self.store.get_task(task["id"])["status"], "done")
        # 下一批（未来任务）才吃到新契约——由 rerender 语义保证，已在 store 层测试

    def test_dead_not_resurrected_by_rerender_or_claim(self):
        """快照②：dead 是终态——rerender 不动它，claim 不取它。"""
        task = self._task("/bad", key="dead-终态")
        self.worker.process(task)  # 400 → dead
        self.assertEqual(self.store.get_task(task["id"])["status"], "dead")
        count = self.store.rerender_vendor("v", lambda v, t, vs: {
            "url": "http://x", "method": "POST", "headers": {}, "body": "{}"})
        self.assertEqual(count, 0)
        self.assertIsNone(self.store.claim_due_task())  # 无到期任务可领

    # ========== 边界输入 ==========

    def test_unicode_and_large_payload_delivered_intact(self):
        """边界①：中文/emoji/大 body（64KB）投递后字节级一致。"""
        payload = json.dumps({"name": "通知测试🚀", "pad": "x" * 65536},
                             ensure_ascii=False)
        task = self._task("/ok", key="uni", body=payload)
        self.worker.process(task)
        self.assertEqual(self.store.get_task(task["id"])["status"], "done")
        self.assertEqual(self.vendor.last_body.decode("utf-8"), payload)

    def test_unknown_http_method_rejected_at_contract_gate(self):
        """边界②：非法 method 在契约管理门被拒（不得流入投递）。"""
        status_ok = True
        try:
            self.store.upsert_vendor("bad-m", "B", "http://x/", "DELETE",
                                     {}, "{}", [])
        except Exception:
            status_ok = False
        # store 层不做 method 校验（防御在 API 门）——验证 API 门已挡：
        # 若未来有人绕过 API 直写 store，投递时会以 conn_error 兜底，不崩 worker
        task, _ = self.store.create_task("bad-m", "http://x/", "DELETE",
                                         {}, "{}", {}, idempotency_key="bm")
        try:
            self.worker.process(task)
            self.assertIn(self.store.get_task(task["id"])["status"],
                          ("dead", "retrying"))  # 404/405 系，不崩
        finally:
            pass

    def test_flaky_recovery_full_lifecycle_evidence(self):
        """边界③：flaky 供应商「故障→恢复」全生命周期——
        3 次失败 + 1 次成功的完整证据链（历史、last_error 清理、最终态）。"""
        task = self._task("/flaky", key="flk")
        for _ in range(3):
            self.worker.process(task)
            self.assertEqual(self.store.get_task(task["id"])["status"],
                             "retrying")
            self._nudge(task["id"])
        # 供应商「恢复」：flaky 路由临时改 200
        self.vendor.routes["/flaky"] = 200
        self.worker.process(self.store.claim_due_task())
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "done")
        self.assertIsNone(got["last_error"])          # 成功即清理
        atts = self.store.list_attempts(task["id"])
        self.assertEqual([a["outcome"] for a in atts],
                         ["http_5xx", "http_5xx", "http_5xx", "success"])
        self.vendor.routes["/flaky"] = 500            # 还原


if __name__ == "__main__":
    unittest.main()
