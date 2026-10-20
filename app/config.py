"""运行配置：重试/超时/轮询参数。

全部参数可被环境变量覆盖（前缀 NDS_），便于测试与部署调优。
默认值依据 docs/ai/sessions/stage2-interrogation-20261020.md 定点 3/4。
"""
import os


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# 重试策略（stage2 定点 3）
RETRY_BASE_SECONDS = _int_env("NDS_RETRY_BASE_SECONDS", 30)
RETRY_FACTOR = _int_env("NDS_RETRY_FACTOR", 2)
MAX_ATTEMPTS = _int_env("NDS_MAX_ATTEMPTS", 7)  # 1 次初始 + 6 次重试

# HTTP 超时（stage2 定点 4）
CONNECT_TIMEOUT_SECONDS = _int_env("NDS_CONNECT_TIMEOUT", 5)
READ_TIMEOUT_SECONDS = _int_env("NDS_READ_TIMEOUT", 10)

# worker 轮询间隔
POLL_INTERVAL_SECONDS = _int_env("NDS_POLL_INTERVAL", 1)

# 优雅停机宽限（须大于读超时，见 stage3 细化版）
GRACEFUL_SHUTDOWN_SECONDS = _int_env("NDS_GRACEFUL_SHUTDOWN", 15)

# 数据库文件路径
DB_PATH = os.environ.get("NDS_DB_PATH", "data/notifications.db")


def backoff_delay(attempts_done: int) -> int:
    """已失败 attempts_done 次后，下一次重试的等待秒数：base * factor^n。"""
    return RETRY_BASE_SECONDS * (RETRY_FACTOR ** (attempts_done - 1))
