"""本地模拟供应商服务（标准库 http.server，零依赖）。

用途：本地联调与 worker 测试。路由：
  POST /ok        → 200
  POST /bad       → 400（模拟请求错误，应被判死）
  POST /error     → 500（模拟对方故障，应重试）
  POST /slow      → 睡 30s 后 200（模拟读超时）
  其余路径        → 404

用法：python scripts/mock_vendor.py [port]   （默认 9901）
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code: int) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 丢弃请求体（模拟对方不细看）
        body = json.dumps({"mock": True, "code": code}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/ok":
            self._respond(200)
        elif self.path == "/bad":
            self._respond(400)
        elif self.path == "/error":
            self._respond(500)
        elif self.path == "/slow":
            time.sleep(30)
            self._respond(200)
        else:
            self._respond(404)

    def log_message(self, *args):  # 静默访问日志
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9901
    print(f"mock vendor listening on :{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
