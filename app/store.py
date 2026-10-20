"""存储层：唯一允许出现 SQL 的模块（stage1 决策——存储可替换的隔离层）。

契约依据：contracts/schema.sql（冻结）。
所有时间一律 UTC ISO8601 字符串。
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from . import config


def _locked(fn):
    """SQLite 连接跨线程共享（FastAPI 线程池 + worker 线程），
    所有公开方法经同一把锁串行化，避免并发访问同一连接。"""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


class Store:
    """SQLite 访问对象。连接被 FastAPI 线程池与 worker 线程共享，
    所有公开方法经 _locked 串行化。"""

    def __init__(self, db_path: str):
        # check_same_thread=False：连接由多线程共享（FastAPI 线程池 + worker），
        # 安全性由 self._lock 串行化保证
        self.conn = sqlite3.connect(db_path, isolation_level=None,  # autocommit
                                    check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")

    @classmethod
    def init(cls, db_path: str) -> "Store":
        """打开数据库并执行建表（schema 冻结契约，幂等）。"""
        ddl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "contracts", "schema.sql")
        with open(ddl_path, encoding="utf-8") as f:
            ddl = f.read()
        conn = sqlite3.connect(db_path)
        conn.executescript(ddl)
        conn.close()
        return cls(db_path)

    @_locked
    def close(self) -> None:
        self.conn.close()

    # ---------- vendors ----------

    @_locked
    def upsert_vendor(self, vendor_id: str, name: str, url_template: str,
                      method: str, headers_template: dict,
                      body_template: str, variables_schema: list) -> None:
        now = _now()
        self.conn.execute(
            """INSERT INTO vendors(vendor_id, name, url_template, method,
                 headers_template, body_template, variables_schema,
                 created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(vendor_id) DO UPDATE SET
                 name=excluded.name, url_template=excluded.url_template,
                 method=excluded.method,
                 headers_template=excluded.headers_template,
                 body_template=excluded.body_template,
                 variables_schema=excluded.variables_schema,
                 updated_at=excluded.updated_at""",
            (vendor_id, name, url_template, method,
             json.dumps(headers_template), body_template,
             json.dumps(variables_schema), now, now),
        )

    @_locked
    def get_vendor(self, vendor_id: str):
        row = self.conn.execute(
            "SELECT * FROM vendors WHERE vendor_id=?", (vendor_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def list_vendors(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM vendors")]

    # ---------- tasks：受理 ----------

    @_locked
    def create_task(self, vendor_id: str, url: str, method: str,
                    headers: dict, body: str, variables: dict,
                    idempotency_key: str | None = None) -> tuple[dict, bool]:
        """插入任务：渲染快照 + 原始变量（ADR-002，rerender 原料）。

        幂等命中时返回 (既有任务, False)；新建返回 (新任务, True)。"""
        if idempotency_key:
            # 幂等键永久唯一（ADR-001）：同 key 一律返回首次任务。
            # 与 schema 的 UNIQUE 全局约束天然一致，无窗口判定。
            existing = self.conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return dict(existing), False
        task_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO tasks(id, vendor_id, url, method, headers_json, body,
                 variables_json, status, attempts, next_attempt_at,
                 idempotency_key, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?, 'pending', 0, ?, ?, ?, ?)""",
            (task_id, vendor_id, url, method, json.dumps(headers), body,
             json.dumps(variables), now, idempotency_key, now, now),
        )
        return self.get_task(task_id), True

    # ---------- tasks：查询 ----------

    @_locked
    def get_task(self, task_id: str):
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def list_tasks(self, status: str | None = None,
                   vendor_id: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if vendor_id:
            sql += " AND vendor_id=?"
            params.append(vendor_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        # 下界挡负数（SQLite LIMIT -1 = 无界），上界按契约封顶 200
        params.append(max(1, min(limit, 200)))
        return [dict(r) for r in self.conn.execute(sql, params)]

    # ---------- tasks：worker 投递生命周期 ----------

    @_locked
    def claim_due_task(self) -> dict | None:
        """取一条到期任务并原子标记 delivering（抢占防重复投递）。

        同一时刻仅本连接可见未提交变更；autocommit 下用两条语句配合
        status 条件更新，保证并发 claim 不会重复取到同一条。
        """
        row = self.conn.execute(
            """SELECT * FROM tasks
               WHERE status IN ('pending','retrying') AND next_attempt_at<=?
               ORDER BY next_attempt_at LIMIT 1""",
            (_now(),),
        ).fetchone()
        if not row:
            return None
        cur = self.conn.execute(
            """UPDATE tasks SET status='delivering', updated_at=?
               WHERE id=? AND status IN ('pending','retrying')""",
            (_now(), row["id"]),
        )
        if cur.rowcount != 1:  # 被其他连接抢先
            return None
        return dict(row)

    @_locked
    def mark_done(self, task_id: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET status='done', last_error=NULL, updated_at=? WHERE id=?",
            (_now(), task_id))

    @_locked
    def mark_dead(self, task_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET status='dead', last_error=?, updated_at=? WHERE id=?",
            (error, _now(), task_id))

    @_locked
    def mark_retrying(self, task_id: str, error: str) -> None:
        """失败后进入重试；超过 MAX_ATTEMPTS 则判死。"""
        task = self.get_task(task_id)
        attempts = task["attempts"]
        if attempts >= config.MAX_ATTEMPTS:
            self.mark_dead(task_id, error)
            return
        delay = config.backoff_delay(attempts)
        next_at = (datetime.now(timezone.utc)
                   + timedelta(seconds=delay)).isoformat()
        self.conn.execute(
            """UPDATE tasks SET status='retrying', last_error=?,
                 next_attempt_at=?, updated_at=? WHERE id=?""",
            (error, next_at, _now(), task_id))

    @_locked
    def recover_delivering(self) -> int:
        """崩溃恢复：把卡在 delivering 的僵尸任务放回重试队列。"""
        cur = self.conn.execute(
            """UPDATE tasks SET status='retrying', updated_at=?
               WHERE status='delivering'""",
            (_now(),),
        )
        return cur.rowcount

    @_locked
    def rerender_vendor(self, vendor_id: str, render_fn) -> int:
        """契约变更收尾：对该 vendor 下 pending/retrying 任务用当前契约重新渲染。

        render_fn(vendor_row, task_row) -> dict(url, method, headers, body)
        渲染原料 = 新契约模板 × 任务行内存储的原始 variables（ADR-002）。
        渲染逻辑由调用方注入，本层只负责筛选与落库。
        """
        vendor = self.get_vendor(vendor_id)
        if not vendor:
            return 0
        rows = self.conn.execute(
            """SELECT * FROM tasks WHERE vendor_id=?
               AND status IN ('pending','retrying')""",
            (vendor_id,),
        ).fetchall()
        count = 0
        for row in rows:
            task = dict(row)
            variables = json.loads(task["variables_json"])
            try:
                rendered = render_fn(vendor, task, variables)
            except Exception:
                # 新契约要求了任务变量之外的新变量等渲染失败：
                # 保持该任务原快照不动，交由运维人工处理
                continue
            self.conn.execute(
                """UPDATE tasks SET url=?, method=?, headers_json=?, body=?,
                     updated_at=? WHERE id=?""",
                (rendered["url"], rendered["method"],
                 json.dumps(rendered["headers"]), rendered["body"],
                 _now(), task["id"]),
            )
            count += 1
        return count

    # ---------- attempts ----------

    @_locked
    def record_attempt(self, task_id: str, attempt_no: int, started_at: str,
                       finished_at: str, outcome: str, status_code: int | None,
                       error_detail: str | None, duration_ms: int | None) -> None:
        self.conn.execute(
            """INSERT INTO task_attempts(id, task_id, attempt_no, started_at,
                 finished_at, outcome, status_code, error_detail, duration_ms)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (_uuid(), task_id, attempt_no, started_at, finished_at,
             outcome, status_code, error_detail, duration_ms),
        )

    @_locked
    def bump_attempts(self, task_id: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET attempts=attempts+1, updated_at=? WHERE id=?",
            (_now(), task_id))

    @_locked
    def list_attempts(self, task_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no",
            (task_id,))]
