"""存储层测试：覆盖 vendors / tasks 生命周期 / attempts / 崩溃恢复 / 幂等。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config
from app.store import Store


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = path
        self.store = Store.init(path)
        self.store.upsert_vendor(
            "ads-a", "广告A", "https://ads.example/{{uid}}", "POST",
            {"X-Token": "{{token}}"}, '{"uid": "{{uid}}"}', ["uid", "token"])

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    # ---------- vendors ----------

    def test_vendor_upsert_and_get(self):
        v = self.store.get_vendor("ads-a")
        self.assertEqual(v["name"], "广告A")
        self.store.upsert_vendor(
            "ads-a", "广告A改", "https://new.example", "POST", {}, "{}", [])
        self.assertEqual(self.store.get_vendor("ads-a")["url_template"],
                         "https://new.example")

    def test_get_vendor_missing(self):
        self.assertIsNone(self.store.get_vendor("nope"))

    # ---------- 受理与幂等 ----------

    def test_create_task_snapshot(self):
        task, created = self.store.create_task(
            "ads-a", "https://ads.example/42", "POST",
            {"X-Token": "t1"}, '{"uid": "42"}', {"uid": "42"},
            idempotency_key="key-1")
        self.assertTrue(created)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["attempts"], 0)
        # 原始变量落库（ADR-002：rerender 原料）
        import json as _json
        self.assertEqual(_json.loads(task["variables_json"]), {"uid": "42"})

    def test_idempotency_hit_within_window(self):
        t1, c1 = self.store.create_task("ads-a", "u", "POST", {}, "{}", {},
                                        idempotency_key="dup-1")
        t2, c2 = self.store.create_task("ads-a", "u", "POST", {}, "{}", {},
                                        idempotency_key="dup-1")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(t1["id"], t2["id"])
        # 不同 key 正常新建
        _, c3 = self.store.create_task("ads-a", "u", "POST", {}, "{}", {},
                                       idempotency_key="dup-2")
        self.assertTrue(c3)

    def test_idempotency_permanent_across_time(self):
        """ADR-001：幂等键永久唯一——即使 created_at 远超任何窗口，同 key
        仍返回首个任务，不产生 IntegrityError，不新建。"""
        task, _ = self.store.create_task("ads-a", "u", "POST", {}, "{}", {},
                                         idempotency_key="old-1")
        ancient = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        self.store.conn.execute("UPDATE tasks SET created_at=? WHERE id=?",
                                (ancient, task["id"]))
        t2, created = self.store.create_task("ads-a", "u", "POST", {}, "{}", {},
                                             idempotency_key="old-1")
        self.assertFalse(created)
        self.assertEqual(t2["id"], task["id"])
        # key 未被篡改，历史完整
        self.assertEqual(self.store.get_task(task["id"])["idempotency_key"], "old-1")

    # ---------- worker 生命周期 ----------

    def _task(self, key="k", variables=None):
        task, _ = self.store.create_task(
            "ads-a", "https://ads.example/cb?uid={{uid}}", "POST", {},
            '{"uid": "{{uid}}"}',
            variables or {"uid": "u1", "token": "tk"},
            idempotency_key=key)
        return task

    def test_claim_and_lifecycle(self):
        task = self._task()
        claimed = self.store.claim_due_task()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], task["id"])
        self.store.mark_done(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "done")
        # done 任务不会再被 claim
        self.assertIsNone(self.store.claim_due_task())

    def test_claim_no_due_task(self):
        self.assertIsNone(self.store.claim_due_task())

    def test_retry_then_dead_on_max_attempts(self):
        task = self._task()
        self.store.claim_due_task()
        # 灌满 MAX_ATTEMPTS 次失败
        for i in range(config.MAX_ATTEMPTS):
            self.store.bump_attempts(task["id"])
            self.store.mark_retrying(task["id"], f"timeout #{i}")
            self.store.claim_due_task()
        self.assertEqual(self.store.get_task(task["id"])["status"], "dead")
        self.assertIn("timeout", self.store.get_task(task["id"])["last_error"])

    def test_backoff_delay_monotonic(self):
        self.store.bump_attempts(self._task()["id"])
        delays = []
        for _ in range(3):
            task = self.store.list_tasks()[0]
            self.store.mark_retrying(task["id"], "e")
            t = self.store.get_task(task["id"])
            delays.append(datetime.fromisoformat(t["next_attempt_at"])
                          - datetime.now(timezone.utc))
            self.store.bump_attempts(task["id"])
        self.assertGreater(delays[1], delays[0])
        self.assertGreater(delays[2], delays[1])

    def test_recover_delivering(self):
        task = self._task()
        self.store.claim_due_task()  # 留在 delivering 模拟崩溃
        recovered = self.store.recover_delivering()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.get_task(task["id"])["status"], "retrying")

    # ---------- attempts ----------

    def test_attempts_history(self):
        task = self._task()
        self.store.claim_due_task()
        self.store.bump_attempts(task["id"])
        self.store.record_attempt(
            task["id"], 1, datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            "timeout", None, "read timeout after 10s", 10021)
        self.store.mark_retrying(task["id"], "read timeout after 10s")
        hist = self.store.list_attempts(task["id"])
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["outcome"], "timeout")

    # ---------- rerender ----------

    def test_rerender_updates_pending_only(self):
        p, _ = self.store.create_task(
            "ads-a", "https://ads.example/cb?uid=u1", "POST", {},
            '{"uid": "u1"}', {"uid": "u1", "token": "tk"},
            idempotency_key="p")
        self.store.claim_due_task()
        self.store.mark_done(p["id"])  # 变 done
        p2, _ = self.store.create_task(
            "ads-a", "https://ads.example/cb?uid=u2", "POST", {},
            '{"uid": "u2"}', {"uid": "u2", "token": "tk"},
            idempotency_key="p2")

        count = self.store.rerender_vendor("ads-a", self._render_via_contract)
        self.assertEqual(count, 1)  # done 不动，只刷 p2
        # p2 快照被重渲染为 契约模板 × p2 的变量
        self.assertEqual(self.store.get_task(p2["id"])["url"],
                         "https://ads.example/u2")
        # p 已 done，保持受理时的原快照
        self.assertEqual(self.store.get_task(p["id"])["url"],
                         "https://ads.example/cb?uid=u1")

    # ---------- 列表 ----------

    def test_list_tasks_filters(self):
        t1 = self._task("a")
        self.store.claim_due_task()          # t1 → delivering
        self.store.mark_dead(t1["id"], "400")
        self._task("b")                      # 保持 pending
        dead = self.store.list_tasks(status="dead")
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["id"], t1["id"])
        self.assertEqual(len(self.store.list_tasks(status="pending")), 1)


    # ---------- 退避公式的精确验证 ----------

    def test_backoff_formula_exact_values(self):
        # base=30, factor=2：等待序列应为 30/60/120/240/480/960
        # （config 默认值；若环境变量被污染，此测试会失败并暴露配置漂移）
        self.assertEqual(config.RETRY_BASE_SECONDS, 30)
        self.assertEqual(config.RETRY_FACTOR, 2)
        expected = [30 * (2 ** n) for n in range(config.MAX_ATTEMPTS - 1)]
        actual = [config.backoff_delay(n + 1) for n in range(config.MAX_ATTEMPTS - 1)]
        self.assertEqual(actual, expected)

    def test_mark_retrying_sets_next_attempt_with_backoff(self):
        task = self._task("bk")
        self.store.claim_due_task()
        self.store.bump_attempts(task["id"])  # attempts=1，已失败 1 次
        before = _utcnow()
        self.store.mark_retrying(task["id"], "500")
        got = self.store.get_task(task["id"])
        delay = (datetime.fromisoformat(got["next_attempt_at"]) - before).total_seconds()
        # 第 1 次失败后应等 base=30s（允许 2s 时钟误差）
        self.assertAlmostEqual(delay, config.RETRY_BASE_SECONDS, delta=2)

    def test_mark_dead_sets_last_error(self):
        task = self._task("de")
        self.store.claim_due_task()
        self.store.mark_dead(task["id"], "HTTP 400 Bad Request")
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "dead")
        self.assertEqual(got["last_error"], "HTTP 400 Bad Request")

    def test_mark_done_clears_last_error(self):
        task = self._task("dc")
        self.store.claim_due_task()
        self.store.mark_dead(task["id"], "500")   # 先失败
        # 场景外无「dead 复活」路径；验证 mark_done 对 last_error 的清理语义
        self.store.mark_done(task["id"])
        self.assertIsNone(self.store.get_task(task["id"])["last_error"])

    # ---------- claim 原子性（跨连接抢占） ----------

    def test_claim_atomic_across_connections(self):
        """模拟两个 worker 连接同时 claim：同一条任务只能被一个抢到。"""
        other = Store(self.path)  # 第二个连接（同库）
        try:
            self._task("race")
            first = self.store.claim_due_task()
            second = other.claim_due_task()
            self.assertIsNotNone(first)
            self.assertIsNone(second, "同一任务被两个连接重复 claim")
        finally:
            other.close()

    def test_claim_picks_earliest_next_attempt(self):
        early, _ = self.store.create_task("ads-a", "u", "POST", {}, "{}", "early")
        late, _ = self.store.create_task("ads-a", "u", "POST", {}, "{}", "late")
        # 把 late 的到期时间推到未来 → 只应 claim 到 early
        future = (_utcnow() + timedelta(hours=1)).isoformat()
        self.store.conn.execute(
            "UPDATE tasks SET next_attempt_at=? WHERE id=?", (future, late["id"]))
        claimed = self.store.claim_due_task()
        self.assertEqual(claimed["id"], early["id"])

    # ---------- 外键与数据完整性 ----------

    def test_task_requires_existing_vendor(self):
        with self.assertRaises(Exception):
            self.store.create_task("ghost-vendor", "u", "POST", {}, "{}", "fk-1")

    def test_attempt_requires_existing_task(self):
        with self.assertRaises(Exception):
            self.store.record_attempt("ghost-task", 1,
                                      _utcnow().isoformat(), None,
                                      "timeout", None, None, None)

    def test_attempts_ordered_by_no(self):
        task = self._task("ord")
        for no in (3, 1, 2):  # 乱序写入
            self.store.record_attempt(task["id"], no,
                                      _utcnow().isoformat(), None,
                                      "http_5xx", 500, "boom", 100)
        nos = [a["attempt_no"] for a in self.store.list_attempts(task["id"])]
        self.assertEqual(nos, [1, 2, 3])

    # ---------- rerender（ADR-002 真渲染语义） ----------

    def _render_via_contract(self, vendor, task, variables):
        from app.render import render_request
        return render_request(vendor, variables)

    def test_rerender_unknown_vendor_returns_zero(self):
        self.assertEqual(
            self.store.rerender_vendor("ghost", self._render_via_contract), 0)

    def test_rerender_does_not_touch_dead(self):
        task = self._task("rd")
        self.store.claim_due_task()
        self.store.mark_dead(task["id"], "400")
        count = self.store.rerender_vendor("ads-a", self._render_via_contract)
        self.assertEqual(count, 0)
        # 原快照不动
        self.assertEqual(self.store.get_task(task["id"])["url"],
                         "https://ads.example/cb?uid={{uid}}")

    def test_rerender_reaches_retrying_tasks(self):
        task = self._task("rr")
        self.store.claim_due_task()
        self.store.bump_attempts(task["id"])
        self.store.mark_retrying(task["id"], "500")  # → retrying
        count = self.store.rerender_vendor("ads-a", self._render_via_contract)
        self.assertEqual(count, 1)
        # 快照 = 契约模板 × 存量变量
        self.assertEqual(self.store.get_task(task["id"])["url"],
                         "https://ads.example/u1")

    def test_rerender_uses_new_template_with_old_variables(self):
        """核心场景（人类确认）：模板变更 → rerender → 新模板 × 旧变量。"""
        task = self._task("e2e")
        # 契约模板升级为 v2（新增路径段）
        self.store.upsert_vendor(
            "ads-a", "广告A", "https://ads.example/v2/cb?uid={{uid}}&src=v2",
            "POST", {}, '{"uid": "{{uid}}"}', ["uid", "token"])
        count = self.store.rerender_vendor("ads-a", self._render_via_contract)
        self.assertEqual(count, 1)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["url"],
                         "https://ads.example/v2/cb?uid=u1&src=v2")
        self.assertEqual(got["body"], '{"uid": "u1"}')

    def test_rerender_skips_task_when_new_template_needs_new_variable(self):
        """新模板要求了任务没有的变量 → 该任务跳过，快照保持原样。"""
        task = self._task("gap")
        self.store.upsert_vendor(
            "ads-a", "广告A", "https://ads.example/cb?uid={{uid}}&sig={{sig}}",
            "POST", {}, "{}", ["uid", "token", "sig"])
        count = self.store.rerender_vendor("ads-a", self._render_via_contract)
        self.assertEqual(count, 0)
        self.assertEqual(self.store.get_task(task["id"])["url"],
                         "https://ads.example/cb?uid={{uid}}")


if __name__ == "__main__":
    unittest.main()
