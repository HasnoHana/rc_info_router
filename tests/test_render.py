"""契约渲染单元测试：占位符替换、变量校验、残留检测。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import render
from app.render import RenderError, render_request, validate_variables


def _vendor(**overrides) -> dict:
    base = {
        "vendor_id": "v1",
        "url_template": "https://api.example/cb?uid={{user_id}}",
        "method": "POST",
        "headers_template": json.dumps({"X-Token": "{{token}}"}),
        "body_template": '{"uid": "{{user_id}}", "ch": "{{channel}}"}',
        "variables_schema": json.dumps(["user_id", "token", "channel"]),
    }
    base.update(overrides)
    return base


class RenderTestCase(unittest.TestCase):

    def test_full_substitution(self):
        out = render_request(_vendor(), {
            "user_id": "u1", "token": "tk", "channel": "tiktok"})
        self.assertEqual(out["url"], "https://api.example/cb?uid=u1")
        self.assertEqual(out["headers"], {"X-Token": "tk"})
        self.assertEqual(out["body"],
                         '{"uid": "u1", "ch": "tiktok"}')
        self.assertEqual(out["method"], "POST")

    def test_missing_variable_rejected(self):
        with self.assertRaises(RenderError) as ctx:
            render_request(_vendor(), {"user_id": "u1"})
        self.assertIn("missing", str(ctx.exception))

    def test_extra_variable_rejected(self):
        with self.assertRaises(RenderError) as ctx:
            render_request(_vendor(), {
                "user_id": "u1", "token": "t", "channel": "c", "junk": 1})
        self.assertIn("unexpected", str(ctx.exception))

    def test_variables_must_match_exactly(self):
        # 空 schema + 空变量 → 合法
        v = _vendor(url_template="https://api.example",
                    headers_template="{}", body_template="",
                    variables_schema="[]")
        out = render_request(v, {})
        self.assertEqual(out["url"], "https://api.example")

    def test_leftover_placeholder_detected(self):
        # 绕过 schema 校验：模板里的占位符不在 schema 中 → 替换后残留 → 报错
        v = _vendor(url_template="https://api.example/{{ghost_var}}",
                    variables_schema="[]")
        with self.assertRaises(RenderError) as ctx:
            render_request(v, {})
        self.assertIn("ghost_var", str(ctx.exception))

    def test_non_string_values_coerced(self):
        out = render_request(_vendor(), {
            "user_id": 12345, "token": "t", "channel": "c"})
        self.assertIn("uid=12345", out["url"])

    def test_header_leftover_placeholder_detected(self):
        # 审查发现：header 模板的残留占位符曾静默通过 → 锁定为报错
        v = _vendor(headers_template=json.dumps({"X-Sig": "{{undeclared_sig}}"}),
                    variables_schema=json.dumps(["user_id", "token", "channel"]))
        with self.assertRaises(RenderError) as ctx:
            render_request(v, {"user_id": "u", "token": "t", "channel": "c"})
        self.assertIn("undeclared_sig", str(ctx.exception))

    def test_unresolved_variable_raises_render_error_not_keyerror(self):
        # 审查发现：_substitute 曾抛裸 KeyError → 锁定为 RenderError
        v = _vendor(url_template="https://api.example/{{ghost_var}}",
                    variables_schema="[]")
        with self.assertRaises(RenderError) as ctx:
            render_request(v, {})
        self.assertIn("ghost_var", str(ctx.exception))

    def test_collect_placeholders(self):
        names = render.collect_placeholders(
            "https://x/{{a}}/y", '{"b": "{{b}}"}', "H-{{c}}")
        self.assertEqual(names, {"a", "b", "c"})


if __name__ == "__main__":
    unittest.main()
