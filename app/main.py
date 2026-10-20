"""进程入口：初始化存储 → 崩溃恢复 → 起 worker 线程 → 起 API。

优雅停机（stage3 细化版）：uvicorn 收到 SIGTERM/SIGINT 自行退出 →
worker 停止领新任务 → 在途投递等自然结束（宽限 > 读超时）→ 进程退出。
"""
import sys
import threading

import uvicorn

from . import config
from .api import create_app
from .store import Store
from .worker import Worker


def main() -> None:
    store = Store.init(config.DB_PATH)
    recovered = store.recover_delivering()
    if recovered:
        print(f"[startup] recovered {recovered} delivering task(s) "
              f"from previous crash", file=sys.stderr)

    worker = Worker(store)
    worker_thread = threading.Thread(target=worker.run, daemon=True,
                                     name="delivery-worker")
    worker_thread.start()

    # 注意：不在 uvicorn.run() 之前注册信号处理器——uvicorn 启动时会覆盖
    # SIGINT/SIGTERM 处理器。优雅停机顺序：uvicorn 收信号自行退出 →
    # run() 返回 → 这里置位 stop_event → worker 停止领新任务 → join 等在途
    # 投递自然结束（宽限 > 读超时，见 stage3 细化版）。
    uvicorn.run(create_app(store), host="127.0.0.1", port=8000,
                log_level="info")

    worker.stop_event.set()
    worker_thread.join(timeout=config.GRACEFUL_SHUTDOWN_SECONDS)
    store.close()


if __name__ == "__main__":
    main()
