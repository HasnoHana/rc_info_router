"""投递 worker：轮询取任务 → HTTP 投递 → 按结果分流（stage2 定点 1/2/3/4）。

单线程串行设计（stage1 决策）：失败模式最简单，崩溃重启后任务仍在表里。
"""
import http.client
import socket
import time
from urllib.parse import urlsplit

from . import config
from .store import Store

# attempt 结果枚举（契约 task_attempts.outcome）
OUTCOME_SUCCESS = "success"
OUTCOME_HTTP_4XX = "http_4xx"
OUTCOME_HTTP_5XX = "http_5xx"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_CONN_ERROR = "conn_error"
OUTCOME_UNKNOWN = "unknown"


def classify_status(status_code: int) -> str:
    if 200 <= status_code < 300:
        return OUTCOME_SUCCESS
    if 300 <= status_code < 400:
        # 契约：不跟随重定向。3xx 是端点配置类问题（如被重定向到登录页），
        # 重试不会自愈，归入判死路径（outcome 枚举无 http_3xx 桶，
        # 复用 http_4xx，真实状态码保留在 error_detail/status_code 字段）
        return OUTCOME_HTTP_4XX
    if 400 <= status_code < 500:
        return OUTCOME_HTTP_4XX
    return OUTCOME_HTTP_5XX


def deliver(task: dict) -> tuple[str, int | None, str | None, int]:
    """执行一次投递。返回 (outcome, status_code, error_detail, duration_ms)。

    超时语义（stage2 定点 4）：连接超时 5s、读超时 10s 分别控制。
    响应体不读取（stage2 定点 1），仅取状态码。
    """
    parts = urlsplit(task["url"])
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, parts.port,
                                           timeout=config.CONNECT_TIMEOUT_SECONDS)
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port,
                                          timeout=config.CONNECT_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        conn.connect()
        # 连接已建立，读阶段切换为读超时
        conn.sock.settimeout(config.READ_TIMEOUT_SECONDS)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request(task["method"], path,
                     body=task["body"].encode("utf-8"),  # http.client 对 str 按
                     # latin-1 编码，中文/emoji 会 UnicodeEncodeError，必须显式 utf-8
                     headers=json_headers(task))
        resp = conn.getresponse()
        status_code = resp.status
        outcome = classify_status(status_code)
        error = None if outcome == OUTCOME_SUCCESS else f"HTTP {status_code}"
        return outcome, status_code, error, _elapsed(started)
    except socket.timeout:
        return OUTCOME_TIMEOUT, None, "connect/read timeout", _elapsed(started)
    except (ConnectionError, OSError) as e:
        return OUTCOME_CONN_ERROR, None, str(e), _elapsed(started)
    except Exception as e:  # 防御：未知异常不拖垮 worker，按 unknown 处理
        return OUTCOME_UNKNOWN, None, repr(e), _elapsed(started)
    finally:
        conn.close()


def json_headers(task: dict) -> dict:
    import json
    headers = json.loads(task["headers_json"])
    headers.setdefault("Content-Type", "application/json")
    return headers


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class Worker:
    """单线程投递循环。stop_event 置位后：不再领取新任务，
    在途投递等自然结束（优雅停机，stage3 细化版）。"""

    def __init__(self, store: Store):
        self.store = store
        import threading
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            task = self.store.claim_due_task()
            if task is None:
                # wait 兼具 sleep 与「立刻响应停机」两个职责
                self.stop_event.wait(config.POLL_INTERVAL_SECONDS)
                continue
            self.process(task)

    def process(self, task: dict) -> None:
        """投递单个任务并按结果分流。独立于 run 便于测试与崩溃恢复。"""
        self.store.bump_attempts(task["id"])
        attempt_no = task["attempts"] + 1
        started_at = _now()
        outcome, status_code, error, duration_ms = deliver(task)
        self.store.record_attempt(
            task["id"], attempt_no, started_at, _now(),
            outcome, status_code, error, duration_ms)
        if outcome == OUTCOME_SUCCESS:
            self.store.mark_done(task["id"])
        elif outcome == OUTCOME_HTTP_4XX:
            # stage2 定点 2：请求本身错误，重试无意义，直接判死
            self.store.mark_dead(task["id"], error or f"HTTP {status_code}")
        else:
            self.store.mark_retrying(task["id"], error or outcome)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
