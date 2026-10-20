"""pydantic 请求模型（契约 contracts/openapi.yaml 的载体）。"""
from pydantic import BaseModel, Field


class VendorIn(BaseModel):
    name: str
    url_template: str
    # method 的枚举校验在端点内显式做（契约要求 400，pydantic 默认 422）
    method: str = "POST"
    headers_template: dict[str, str] = {}
    body_template: str = ""
    variables_schema: list[str] = []


class NotificationIn(BaseModel):
    vendor_id: str
    variables: dict[str, object]
    idempotency_key: str | None = Field(default=None, max_length=128)
