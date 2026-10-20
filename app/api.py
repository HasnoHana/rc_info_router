"""FastAPI 受理与查询层。契约：contracts/openapi.yaml（冻结）。

本层只做：请求模型校验 → 渲染 → 委托 store。无 SQL、无投递逻辑。
"""
import json

from fastapi import FastAPI, HTTPException, Request

from . import render
from .models import NotificationIn, VendorIn
from .store import Store


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="Internal Notification Delivery Service",
                  version="0.1.0")
    app.state.store = store

    # ---------- 受理 ----------

    @app.post("/notifications", status_code=202)
    def submit_notification(payload: NotificationIn, request: Request):
        store: Store = request.app.state.store
        vendor = store.get_vendor(payload.vendor_id)
        if not vendor:
            raise HTTPException(400, f"unknown vendor '{payload.vendor_id}'")
        try:
            rendered = render.render_request(vendor, payload.variables)
        except render.RenderError as e:
            raise HTTPException(400, str(e))
        task, created = store.create_task(
            payload.vendor_id, rendered["url"], rendered["method"],
            rendered["headers"], rendered["body"], payload.variables,
            idempotency_key=payload.idempotency_key)
        return _task_view(task)

    # ---------- vendors ----------

    @app.get("/vendors")
    def list_vendors(request: Request):
        return [ _vendor_view(v) for v in request.app.state.store.list_vendors() ]

    @app.put("/vendors/{vendor_id}")
    def put_vendor(vendor_id: str, payload: VendorIn, request: Request):
        if payload.method not in ("POST", "PUT", "PATCH"):
            raise HTTPException(400, f"invalid method '{payload.method}' "
                                     f"(allowed: POST, PUT, PATCH)")
        # 模板合法性校验（契约：模板格式非法 400）——
        # 错误必须在契约管理时刻拦下，而不是推迟到投递时暴露
        if not payload.url_template.startswith(("http://", "https://")):
            raise HTTPException(400, "url_template must start with "
                                     "http:// or https://")
        schema = set(payload.variables_schema)
        placeholders = render.collect_placeholders(
            payload.url_template, payload.body_template,
            *payload.headers_template.values())
        unknown = placeholders - schema
        if unknown:
            raise HTTPException(400, f"placeholders {sorted(unknown)} not "
                                     f"declared in variables_schema")
        store: Store = request.app.state.store
        store.upsert_vendor(vendor_id, payload.name, payload.url_template,
                            payload.method, payload.headers_template,
                            payload.body_template, payload.variables_schema)
        return {"vendor_id": vendor_id, "saved": True}

    @app.get("/vendors/{vendor_id}")
    def get_vendor(vendor_id: str, request: Request):
        vendor = request.app.state.store.get_vendor(vendor_id)
        if not vendor:
            raise HTTPException(404, f"vendor '{vendor_id}' not found")
        return _vendor_view(vendor)

    @app.post("/vendors/{vendor_id}/rerender")
    def rerender(vendor_id: str, request: Request):
        """契约变更收尾：新模板 × 任务行内存量变量（ADR-002）重渲染在途任务。

        done 不动（既成事实）；dead 不自动复活（需人工确认后重提）。
        新契约模板要求了任务变量之外的新变量时，该任务渲染失败——
        保持原快照不动，计入 unresolved 返回给调用方处理。
        """
        store: Store = request.app.state.store
        if not store.get_vendor(vendor_id):
            raise HTTPException(404, f"vendor '{vendor_id}' not found")

        def render_fn(vendor: dict, task: dict, variables: dict) -> dict:
            return render.render_request(vendor, variables)

        count = store.rerender_vendor(vendor_id, render_fn)
        return {"rerendered": count}

    # ---------- tasks ----------

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str, request: Request):
        task = request.app.state.store.get_task(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        return _task_view(task)

    @app.get("/tasks")
    def list_tasks(request: Request, status: str | None = None,
                   vendor_id: str | None = None, limit: int = 50):
        if status is not None and status not in (
                "pending", "delivering", "retrying", "done", "dead"):
            raise HTTPException(400, f"invalid status '{status}'")
        if limit < 1:
            raise HTTPException(400, f"invalid limit {limit} (must be >= 1)")
        return [_task_view(t) for t in request.app.state.store.list_tasks(
            status, vendor_id, min(limit, 200))]

    @app.get("/tasks/{task_id}/attempts")
    def list_attempts(task_id: str, request: Request):
        if not request.app.state.store.get_task(task_id):
            raise HTTPException(404, "task not found")
        return request.app.state.store.list_attempts(task_id)

    return app


def _vendor_view(v: dict) -> dict:
    return {
        "vendor_id": v["vendor_id"], "name": v["name"],
        "url_template": v["url_template"], "method": v["method"],
        "headers_template": json.loads(v["headers_template"]),
        "body_template": v["body_template"],
        "variables_schema": json.loads(v["variables_schema"]),
        "created_at": v["created_at"], "updated_at": v["updated_at"],
    }


def _task_view(t: dict) -> dict:
    return {
        "id": t["id"], "vendor_id": t["vendor_id"], "status": t["status"],
        "attempts": t["attempts"], "next_attempt_at": t["next_attempt_at"],
        "idempotency_key": t["idempotency_key"],
        "last_error": t.get("last_error"),
        "created_at": t["created_at"], "updated_at": t["updated_at"],
    }
