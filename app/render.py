"""契约渲染： vendors 模板 + 业务方变量 → 最终请求（受理时快照）。

模板能力边界（stage3 定稿）：仅 {{var}} 占位符替换。
不做条件逻辑、函数、映射；变量缺失或多余均视为非法。
"""
import re
import json

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_LEFTOVER = re.compile(r"\{\{.*?\}\}", re.DOTALL)


class RenderError(ValueError):
    """渲染失败：变量缺失/多余/残留占位符。受理层转 400。"""


def validate_variables(vendor: dict, variables: dict) -> None:
    """按契约 variables_schema 校验：集合必须精确相等。"""
    required = set(json.loads(vendor["variables_schema"]))
    provided = set(variables.keys())
    missing = required - provided
    extra = provided - required
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing: {sorted(missing)}")
        if extra:
            parts.append(f"unexpected: {sorted(extra)}")
        raise RenderError(f"variables mismatch for vendor "
                          f"'{vendor['vendor_id']}': " + "; ".join(parts))


def _substitute(template: str, variables: dict) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in variables:
            raise RenderError(f"unresolved variable '{name}' "
                              f"(not in provided variables)")
        return str(variables[name])
    return _PLACEHOLDER.sub(repl, template)


def collect_placeholders(*templates: str) -> set[str]:
    """提取模板集合中出现的全部占位符名（契约管理期校验用）。"""
    names: set[str] = set()
    for t in templates:
        names.update(m.group(1) for m in _PLACEHOLDER.finditer(t))
    return names


def render_request(vendor: dict, variables: dict) -> dict:
    """渲染最终请求。返回 {url, method, headers, body}。

    vendor 的 headers_template/body_template 为 JSON 字符串（schema 冻结）。
    """
    validate_variables(vendor, variables)
    url = _substitute(vendor["url_template"], variables)
    headers = {k: _substitute(v, variables)
               for k, v in json.loads(vendor["headers_template"]).items()}
    body = _substitute(vendor["body_template"], variables)
    # 残留占位符检查必须覆盖 url + 全部 header 值 + body
    for text in (url, body, *headers.values()):
        leftover = _LEFTOVER.search(text)
        if leftover:
            raise RenderError(
                f"unresolved placeholder {leftover.group(0)!r} after substitution "
                f"(check variables_schema covers all template vars)")
    return {"url": url, "method": vendor["method"],
            "headers": headers, "body": body}
