"""CLI 接入工具（stage2 定点 6+）：业务方提交通知的命令行客户端。

标准库实现（urllib + argparse），零第三方依赖。

用法示例：
  # 提交（变量可重复传入，或用 --vars-file 给 JSON 文件）
  python scripts/submit_notification.py submit \
      --vendor ads-a --var user_id=u1 --var token=tk \
      --idempotency-key evt-001

  # 查询任务状态与投递历史
  python scripts/submit_notification.py status --task <task_id>

  # 交互式填空：按契约 variables_schema 逐项提问后提交
  python scripts/submit_notification.py submit --vendor ads-a --interactive
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_SERVER = "http://127.0.0.1:8000"


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except urllib.error.URLError as e:
        print(f"[error] 无法连接服务 {url}：{e.reason}", file=sys.stderr)
        sys.exit(2)


def load_variables(args) -> dict:
    variables = {}
    for kv in args.var or []:
        if "=" not in kv:
            print(f"[error] --var 格式应为 key=value，收到：{kv}", file=sys.stderr)
            sys.exit(2)
        k, v = kv.split("=", 1)
        variables[k] = v
    if args.vars_file:
        with open(args.vars_file, encoding="utf-8") as f:
            variables.update(json.load(f))
    return variables


def interactive_fill(server: str, vendor_id: str) -> dict:
    """按契约 variables_schema 逐项提问（stage2「表单式填写」的 CLI 形态）。"""
    status, vendor = _request("GET", f"{server}/vendors/{vendor_id}")
    if status != 200:
        print(f"[error] 契约查询失败({status})：{vendor}", file=sys.stderr)
        sys.exit(2)
    print(f"契约 {vendor_id}（{vendor['name']}）需要以下变量：")
    variables = {}
    for name in vendor["variables_schema"]:
        variables[name] = input(f"  {name} = ").strip()
    print(f"  url 模板: {vendor['url_template']}")
    return variables


def cmd_submit(args) -> None:
    variables = interactive_fill(args.server, args.vendor) \
        if args.interactive else load_variables(args)
    payload = {"vendor_id": args.vendor, "variables": variables}
    if args.idempotency_key:
        payload["idempotency_key"] = args.idempotency_key
    status, resp = _request("POST", f"{args.server}/notifications", payload)
    if status != 202:
        print(f"[error] 受理失败({status})：{resp.get('detail', resp)}",
              file=sys.stderr)
        sys.exit(1)
    task_id = resp["id"]
    print(f"已受理 task_id={task_id} status={resp['status']}")
    if args.watch:
        print("等待投递结果（Ctrl+C 退出）...")
        while True:
            time.sleep(2)
            _, task = _request("GET", f"{args.server}/tasks/{task_id}")
            print(f"  status={task['status']} attempts={task['attempts']}"
                  + (f" last_error={task['last_error']}" if task["last_error"] else ""))
            if task["status"] in ("done", "dead"):
                break


def cmd_status(args) -> None:
    status, task = _request("GET", f"{args.server}/tasks/{args.task}")
    if status != 200:
        print(f"[error] {status}：{task}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(task, ensure_ascii=False, indent=2))
    status, attempts = _request("GET",
                                f"{args.server}/tasks/{args.task}/attempts")
    if attempts:
        print("投递历史：")
        for a in attempts:
            print(f"  #{a['attempt_no']} {a['outcome']} "
                  f"code={a['status_code']} {a['error_detail'] or ''} "
                  f"({a['duration_ms']}ms)")


def main() -> None:
    parser = argparse.ArgumentParser(description="通知提交 CLI")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"服务地址（默认 {DEFAULT_SERVER}）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="提交通知")
    p_submit.add_argument("--vendor", required=True, help="vendor_id")
    p_submit.add_argument("--var", action="append", metavar="K=V",
                          help="契约变量，可重复")
    p_submit.add_argument("--vars-file", help="变量 JSON 文件")
    p_submit.add_argument("--idempotency-key", help="幂等键（防重贴标签）")
    p_submit.add_argument("--interactive", action="store_true",
                          help="按契约逐项填空")
    p_submit.add_argument("--watch", action="store_true",
                          help="提交后轮询直到 done/dead")
    p_submit.set_defaults(func=cmd_submit)

    p_status = sub.add_parser("status", help="查询任务状态与历史")
    p_status.add_argument("--task", required=True, help="task_id")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
