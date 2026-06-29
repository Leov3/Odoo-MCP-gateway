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

from app.mcp_server import (
    _expand_product_queries,
    _is_clear_confirmation,
    _or_domain,
    _strip_purchase_intent,
    _validate_limit,
    buscar_cliente_por_telefono,
    buscar_producto_venta,
    confirmar_cotizacion_whatsapp,
    consultar_disponibilidad,
    consultar_estado_pedido,
    crear_actividad_para_vendedor,
    crear_cotizacion_whatsapp,
    crear_o_actualizar_cliente_whatsapp,
    actualizar_cotizacion_whatsapp,
)
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


class FakeOdooClient:
    def __init__(self) -> None:
        self.records: dict[str, dict[int, dict[str, object]]] = {}
        self.calls: list[tuple[str, str, list[int] | None, list[object] | None, dict[str, object] | None]] = []
        self._next_id = 1

    def create(self, model: str, values: dict[str, object]) -> int:
        record_id = self._next_id
        self._next_id += 1
        record = dict(values)
        record["id"] = record_id
        if model == "sale.order":
            record.setdefault("state", "draft")
            record.setdefault("note", "")
        self.records.setdefault(model, {})[record_id] = record
        return record_id

    def read(self, model: str, ids: list[int], fields: list[str], load=None):  # noqa: ANN001
        result: list[dict[str, object]] = []
        for record_id in ids:
            record = self.records.get(model, {}).get(int(record_id))
            if not record:
                continue
            payload = {field: record.get(field) for field in fields}
            payload["id"] = record_id
            result.append(payload)
        return result

    def search_read(self, model: str, domain, fields: list[str], limit: int = 10):  # noqa: ANN001
        if model == "sale.order.line":
            order_id = None
            for token in domain:
                if isinstance(token, tuple) and token[0] == "order_id":
                    order_id = int(token[2])
            records = []
            for record in self.records.get(model, {}).values():
                if order_id is not None and int(record.get("order_id") or 0) != order_id:
                    continue
                payload = {field: record.get(field) for field in fields}
                payload["id"] = record["id"]
                records.append(payload)
            return records[:limit]
        return []

    def write(self, model: str, ids: list[int], values: dict[str, object]) -> bool:
        for record_id in ids:
            record = self.records.setdefault(model, {}).setdefault(int(record_id), {"id": int(record_id)})
            record.update(values)
        return True

    def call_method(self, model: str, method: str, ids: list[int] | None = None, args: list[object] | None = None, kwargs: dict[str, object] | None = None):  # noqa: ANN001
        self.calls.append((model, method, ids, args, kwargs))
        if model == "sale.order" and method == "action_confirm" and ids:
            for record_id in ids:
                self.records.setdefault(model, {}).setdefault(int(record_id), {"id": int(record_id)})["state"] = "sale"
        return {"model": model, "method": method, "ids": ids, "kwargs": kwargs}


def _fake_request(instance_slug: str = "compraloahora") -> _FakeRequest:
    req = _FakeRequest()
    req.state = types.SimpleNamespace(
        odoo_instance={
            "url": "https://example.com",
            "database_name": "db",
            "username": "demo",
            "secret_encrypted": "encrypted",
            "version": "17",
            "api_mode": "xmlrpc",
            "slug": instance_slug,
            "name": instance_slug,
        }
    )
    return req


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

    def test_json2_partner_create_sends_customer_rank(self) -> None:
        client = self._make_client(api_mode="json2")

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse(123)

        with patch("app.odoo_client.requests.post", side_effect=fake_post):
            result = client.create("res.partner", {"name": "Cliente Demo", "customer_rank": 1})

        self.assertEqual(result, 123)
        self.assertIn("/json/2/res.partner/create", captured["url"])
        self.assertEqual(captured["json"], {"vals_list": [{"name": "Cliente Demo", "customer_rank": 1}]})

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


class MarioBusinessToolTests(unittest.TestCase):
    def test_is_clear_confirmation_detects_explicit_yes(self) -> None:
        self.assertTrue(_is_clear_confirmation("Sí, adelante"))
        self.assertFalse(_is_clear_confirmation("quizás luego"))

    def test_buscar_cliente_por_telefono_returns_best_match(self) -> None:
        request = _fake_request()
        with patch("app.mcp_server._client_from_instance", return_value=FakeOdooClient()), patch(
            "app.mcp_server._search_partners_by_phone",
            return_value=[
                {"id": 7, "name": "Cliente Demo", "phone": "3001234567", "mobile": "", "match_score": 200.0}
            ],
        ):
            result = buscar_cliente_por_telefono("3001234567", request=request)

        self.assertTrue(result["success"])
        self.assertEqual(result["best_match"]["id"], 7)

    def test_crear_o_actualizar_cliente_whatsapp_creates_partner(self) -> None:
        request = _fake_request()
        fake_client = FakeOdooClient()
        with patch("app.mcp_server._client_from_instance", return_value=fake_client), patch(
            "app.mcp_server._search_partner_by_phone_best",
            return_value=None,
        ):
            result = crear_o_actualizar_cliente_whatsapp(
                phone="3001234567",
                name="Cliente WhatsApp",
                email="cliente@example.com",
                request=request,
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["record"]["name"], "Cliente WhatsApp")
        self.assertEqual(result["record"]["phone"], "3001234567")

    def test_buscar_producto_venta_returns_max_three(self) -> None:
        request = _fake_request()
        fake_template_results = [
            {"id": 1, "name": "Producto 1", "display_name": "Producto 1", "list_price": 10.0, "record_model": "product.template", "relevance": 100.0},
            {"id": 2, "name": "Producto 2", "display_name": "Producto 2", "list_price": 20.0, "record_model": "product.template", "relevance": 90.0},
            {"id": 3, "name": "Producto 3", "display_name": "Producto 3", "list_price": 30.0, "record_model": "product.template", "relevance": 80.0},
            {"id": 4, "name": "Producto 4", "display_name": "Producto 4", "list_price": 40.0, "record_model": "product.template", "relevance": 70.0},
        ]
        fake_variant_results: list[dict[str, object]] = []
        with patch("app.mcp_server._client_from_instance", return_value=FakeOdooClient()), patch(
            "app.mcp_server._search_category_matches",
            return_value=[],
        ), patch(
            "app.mcp_server._search_product_records",
            side_effect=[fake_template_results, fake_variant_results],
        ):
            result = buscar_producto_venta("producto", request=request)

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 3)

    def test_consultar_disponibilidad_uses_stock_fields(self) -> None:
        request = _fake_request()
        fake_client = FakeOdooClient()
        with patch("app.mcp_server._client_from_instance", return_value=fake_client), patch(
            "app.mcp_server._read_single_record",
            return_value={"id": 9, "display_name": "Producto", "qty_available": 5, "free_qty": 5, "virtual_available": 7},
        ):
            result = consultar_disponibilidad(9, quantity=3, request=request)

        self.assertTrue(result["success"])
        self.assertEqual(result["available_qty"], 5.0)
        self.assertTrue(result["can_fulfill"])

    def test_crear_cotizacion_whatsapp_creates_order_and_lines(self) -> None:
        request = _fake_request()
        fake_client = FakeOdooClient()
        fake_client.records["res.partner"] = {
            10: {"id": 10, "name": "Cliente Demo", "phone": "3001234567", "mobile": "3001234567", "email": "demo@example.com"}
        }
        with patch("app.mcp_server._client_from_instance", return_value=fake_client):
            result = crear_cotizacion_whatsapp(
                partner_id=10,
                items=[{"product_id": 101, "product_model": "product.product", "quantity": 3}],
                whatsapp_phone="3001234567",
                notes="Origen desde WhatsApp",
                request=request,
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["order"]["origin"], "WhatsApp 3001234567")
        self.assertIn("Origen: WhatsApp 3001234567", result["order"]["note"])
        self.assertEqual(result["line_count"], 1)

    def test_actualizar_cotizacion_whatsapp_updates_existing_line(self) -> None:
        request = _fake_request()
        fake_client = FakeOdooClient()
        fake_client.records["sale.order"] = {
            1: {"id": 1, "partner_id": 10, "state": "draft", "note": "", "origin": "WhatsApp 3001234567", "client_order_ref": "WhatsApp 3001234567"}
        }
        fake_client.records["sale.order.line"] = {
            2: {"id": 2, "order_id": 1, "product_id": 101, "product_uom_qty": 1.0, "name": "Producto"}
        }
        with patch("app.mcp_server._client_from_instance", return_value=fake_client):
            result = actualizar_cotizacion_whatsapp(
                order_id=1,
                items=[{"product_id": 101, "product_model": "product.product", "quantity": 4}],
                request=request,
            )

        self.assertTrue(result["updated"])
        self.assertEqual(fake_client.records["sale.order.line"][2]["product_uom_qty"], 4.0)

    def test_confirmar_cotizacion_whatsapp_requires_explicit_text(self) -> None:
        request = _fake_request()
        fake_client = FakeOdooClient()
        fake_client.records["sale.order"] = {
            1: {"id": 1, "partner_id": 10, "state": "draft", "note": "", "origin": "WhatsApp 3001234567", "client_order_ref": "WhatsApp 3001234567"}
        }
        fake_client.records["sale.order.line"] = {
            2: {"id": 2, "order_id": 1, "product_id": 101, "product_uom_qty": 1.0, "name": "Producto"}
        }
        with patch("app.mcp_server._client_from_instance", return_value=fake_client):
            rejected = confirmar_cotizacion_whatsapp(
                order_id=1,
                confirmation_text="tal vez",
                whatsapp_phone="3001234567",
                request=request,
            )
            accepted = confirmar_cotizacion_whatsapp(
                order_id=1,
                confirmation_text="Sí, adelante",
                whatsapp_phone="3001234567",
                request=request,
            )

        self.assertFalse(rejected["success"])
        self.assertTrue(accepted["confirmed"])
        self.assertEqual(fake_client.records["sale.order"][1]["state"], "sale")

    def test_consultar_estado_pedido_returns_results(self) -> None:
        request = _fake_request()
        fake_client = FakeOdooClient()
        with patch("app.mcp_server._client_from_instance", return_value=fake_client), patch.object(
            fake_client,
            "search_read",
            return_value=[
                {"id": 1, "name": "S0001", "partner_id": 10, "client_order_ref": "WhatsApp 3001234567", "origin": "WhatsApp 3001234567", "amount_total": 120.0, "state": "draft", "date_order": "2026-06-29", "currency_id": 1, "activity_state": "overdue"}
            ],
        ):
            result = consultar_estado_pedido("3001234567", request=request)

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)

    def test_crear_actividad_para_vendedor_schedules_activity(self) -> None:
        request = _fake_request()
        fake_client = FakeOdooClient()
        with patch("app.mcp_server._client_from_instance", return_value=fake_client), patch(
            "app.mcp_server._find_activity_type_id",
            return_value=7,
        ):
            result = crear_actividad_para_vendedor(
                partner_id=10,
                summary="Seguimiento",
                reason="Cliente pidió hablar con un humano",
                whatsapp_phone="3001234567",
                request=request,
            )

        self.assertTrue(result["success"])
        self.assertEqual(fake_client.calls[0][1], "activity_schedule")


if __name__ == "__main__":
    unittest.main()
