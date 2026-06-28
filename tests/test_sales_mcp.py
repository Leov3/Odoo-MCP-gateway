from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

fastmcp_module = types.ModuleType("fastmcp")
fastmcp_dependencies = types.ModuleType("fastmcp.dependencies")


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, func=None):
        def decorator(fn):
            return fn

        if func is None:
            return decorator
        return decorator(func)


def _current_request():
    return None


fastmcp_module.FastMCP = _FakeFastMCP
fastmcp_dependencies.CurrentRequest = _current_request
fastmcp_module.dependencies = fastmcp_dependencies
sys.modules.setdefault("fastmcp", fastmcp_module)
sys.modules.setdefault("fastmcp.dependencies", fastmcp_dependencies)

starlette_module = types.ModuleType("starlette")
starlette_requests = types.ModuleType("starlette.requests")


class _FakeRequest:
    state = types.SimpleNamespace()


starlette_requests.Request = _FakeRequest
starlette_module.requests = starlette_requests
sys.modules.setdefault("starlette", starlette_module)
sys.modules.setdefault("starlette.requests", starlette_requests)

security_module = types.ModuleType("app.security")
security_module.decrypt_secret = lambda value: "token"
sys.modules.setdefault("app.security", security_module)

from app.mcp_server import _expand_product_queries, _or_domain, _strip_purchase_intent, _validate_limit
from app.odoo_client import OdooClient, OdooInstanceConfig


class FakeResponse:
    def __init__(self, payload: object, ok: bool = True):
        self._payload = payload
        self.ok = ok
        self.status_code = 200
        self.text = ""

    def json(self) -> object:
        return self._payload


class FakeProxy:
    def __init__(self, result: object = True):
        self.result = result
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def execute_kw(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class SalesMcpHelperTests(unittest.TestCase):
    def test_strip_purchase_intent(self) -> None:
        self.assertEqual(_strip_purchase_intent("quiero un abrelatas"), "abrelatas")

    def test_expand_product_queries_includes_synonyms(self) -> None:
        variants = _expand_product_queries("abrelatas")
        self.assertIn("abridor de latas", variants)
        self.assertIn("can opener", variants)

    def test_validate_limit_clamps(self) -> None:
        self.assertEqual(_validate_limit(99), 50)

    def test_or_domain_prefixes_conditions(self) -> None:
        domain = _or_domain(
            [
                ("name", "ilike", "uno"),
                ("barcode", "ilike", "uno"),
                ("default_code", "ilike", "uno"),
            ]
        )
        self.assertEqual(domain[0], "|")
        self.assertEqual(domain[-1], ("default_code", "ilike", "uno"))


class OdooClientRpcTests(unittest.TestCase):
    def _make_client(self, api_mode: str = "xmlrpc") -> OdooClient:
        with patch("app.odoo_client.decrypt_secret", return_value="token"):
            config = OdooInstanceConfig(
                url="https://example.com",
                database_name="test_db",
                username="demo",
                secret_encrypted="encrypted",
                version="17",
                api_mode=api_mode,
            )
            client = OdooClient(config)
            client._uid = 7
            return client

    def test_xmlrpc_write_uses_execute_kw(self) -> None:
        client = self._make_client()
        proxy = FakeProxy(result=True)
        client._xmlrpc_object = lambda: proxy  # type: ignore[method-assign]

        result = client.write("res.partner", [1], {"name": "Nuevo"})

        self.assertTrue(result)
        self.assertEqual(len(proxy.calls), 1)
        args, kwargs = proxy.calls[0]
        self.assertEqual(args[3], "res.partner")
        self.assertEqual(args[4], "write")
        self.assertEqual(args[5], [[1], {"name": "Nuevo"}])
        self.assertEqual(kwargs, {})

    def test_xmlrpc_call_method_uses_ids_and_kwargs(self) -> None:
        client = self._make_client()
        proxy = FakeProxy(result={"ok": True})
        client._xmlrpc_object = lambda: proxy  # type: ignore[method-assign]

        result = client.call_method("sale.order", "action_confirm", ids=[10], kwargs={"foo": "bar"})

        self.assertEqual(result, {"ok": True})
        args, kwargs = proxy.calls[0]
        self.assertEqual(args[3], "sale.order")
        self.assertEqual(args[4], "action_confirm")
        self.assertEqual(args[5], [[10]])
        self.assertEqual(args[6], {"foo": "bar"})
        self.assertEqual(kwargs, {})

    def test_json2_create_sends_vals_list(self) -> None:
        client = self._make_client(api_mode="json2")

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse(42)

        with patch("app.odoo_client.requests.post", side_effect=fake_post):
            result = client.create("sale.order", {"partner_id": 5})

        self.assertEqual(result, 42)
        self.assertIn("/json/2/sale.order/create", captured["url"])
        self.assertEqual(captured["json"], {"vals_list": [{"partner_id": 5}]})

    def test_json2_write_sends_ids_and_vals(self) -> None:
        client = self._make_client(api_mode="json2")

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse(True)

        with patch("app.odoo_client.requests.post", side_effect=fake_post):
            result = client.write("res.partner", [1, 2], {"name": "Demo"})

        self.assertTrue(result)
        self.assertIn("/json/2/res.partner/write", captured["url"])
        self.assertEqual(captured["json"], {"ids": [1, 2], "vals": {"name": "Demo"}, "context": {"lang": "en_US"}})


if __name__ == "__main__":
    unittest.main()
