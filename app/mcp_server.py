from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentRequest
from starlette.requests import Request

from app.odoo_client import OdooClient, OdooInstanceConfig

MAX_LIMIT = 50

mcp = FastMCP("Odoo MCP Gateway")


def _tool_error(message: str, **extra: Any) -> dict[str, Any]:
    payload = {"success": False, "message": message}
    payload.update(extra)
    return payload


def _instance_from_request(request: Request) -> dict[str, Any]:
    instance = getattr(request.state, "odoo_instance", None)
    if not isinstance(instance, dict):
        raise ValueError("MCP instance context is missing")
    return instance


def _client_from_instance(instance: dict[str, Any]) -> OdooClient:
    config = OdooInstanceConfig(
        url=instance["url"],
        database_name=instance["database_name"],
        username=instance["username"],
        secret_encrypted=instance["secret_encrypted"],
        version=instance["version"],
        api_mode=instance["api_mode"],
    )
    return OdooClient(config)


@mcp.tool
def odoo_test_connection(request: Request = CurrentRequest()) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        client.authenticate()
        return {"success": True, "message": f'Connection to "{instance["name"]}" succeeded'}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_partners(
    query: str,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        query_value = (query or "").strip()
        if not query_value:
            return _tool_error("Query cannot be empty")
        limit_value = max(1, min(int(limit), MAX_LIMIT))
        client = _client_from_instance(instance)
        results = client.search_read(
            "res.partner",
            ["|", "|", "|", ["name", "ilike", query_value], ["email", "ilike", query_value], ["phone", "ilike", query_value], ["mobile", "ilike", query_value]],
            ["id", "name", "email", "phone", "mobile", "company_type"],
            limit=limit_value,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_leads(
    query: str,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        query_value = (query or "").strip()
        if not query_value:
            return _tool_error("Query cannot be empty")
        limit_value = max(1, min(int(limit), MAX_LIMIT))
        client = _client_from_instance(instance)
        results = client.search_read(
            "crm.lead",
            ["|", "|", ["name", "ilike", query_value], ["contact_name", "ilike", query_value], ["email_from", "ilike", query_value]],
            ["id", "name", "contact_name", "email_from", "phone", "stage_id"],
            limit=limit_value,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_create_lead(
    name: str,
    contact_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    description: str | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        if not (name or "").strip():
            return _tool_error("Lead name cannot be empty")
        client = _client_from_instance(instance)
        values: dict[str, Any] = {"name": name.strip()}
        if contact_name:
            values["contact_name"] = contact_name.strip()
        if email:
            values["email_from"] = email.strip()
        if phone:
            values["phone"] = phone.strip()
        if description:
            values["description"] = description.strip()
        lead_id = client.create("crm.lead", values)
        return {"created": True, "lead_id": lead_id}
    except Exception as exc:
        return {"created": False, "message": str(exc)}


@mcp.tool
def odoo_search_sale_orders(
    query: str,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        query_value = (query or "").strip()
        if not query_value:
            return _tool_error("Query cannot be empty")
        limit_value = max(1, min(int(limit), MAX_LIMIT))
        client = _client_from_instance(instance)
        results = client.search_read(
            "sale.order",
            ["|", ["name", "ilike", query_value], ["client_order_ref", "ilike", query_value]],
            ["id", "name", "partner_id", "amount_total", "state", "date_order"],
            limit=limit_value,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))
