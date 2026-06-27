from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from app.db import get_instance_by_name, list_instances
from app.odoo_client import OdooClient, OdooClientError, OdooInstanceConfig

MAX_LIMIT = 50

mcp = FastMCP("Odoo MCP Gateway")


def _tool_error(message: str, **extra: Any) -> dict[str, Any]:
    payload = {"success": False, "message": message}
    payload.update(extra)
    return payload


def _require_active_instance(name: str) -> dict[str, Any]:
    instance = get_instance_by_name(name)
    if not instance:
        raise ValueError(f'Instance "{name}" was not found')
    if not instance["active"]:
        raise ValueError(f'Instance "{name}" is inactive')
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


def _search_domain(query: str, fields: list[str]) -> list[Any]:
    domain: list[Any] = []
    for index, field in enumerate(fields):
        condition = [field, "ilike", query]
        if index == 0:
            domain.append(condition)
        else:
            domain = ["|", domain, condition] if domain else [condition]
    return domain


@mcp.tool
def odoo_list_instances() -> list[dict[str, Any]]:
    return [
        {
            "name": instance["name"],
            "url": instance["url"],
            "version": instance["version"],
            "api_mode": instance["api_mode"],
        }
        for instance in list_instances(active_only=True)
    ]


@mcp.tool
def odoo_test_connection(instance: str) -> dict[str, Any]:
    try:
        record = _require_active_instance(instance)
        client = _client_from_instance(record)
        client.authenticate()
        return {"success": True, "message": f'Connection to "{instance}" succeeded'}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_partners(instance: str, query: str, limit: int = 10) -> dict[str, Any]:
    try:
        record = _require_active_instance(instance)
        query = (query or "").strip()
        if not query:
            return _tool_error("Query cannot be empty")
        limit = max(1, min(int(limit), MAX_LIMIT))
        client = _client_from_instance(record)
        results = client.search_read(
            "res.partner",
            ["|", "|", "|", ["name", "ilike", query], ["email", "ilike", query], ["phone", "ilike", query], ["mobile", "ilike", query]],
            ["id", "name", "email", "phone", "mobile", "company_type"],
            limit=limit,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_leads(instance: str, query: str, limit: int = 10) -> dict[str, Any]:
    try:
        record = _require_active_instance(instance)
        query = (query or "").strip()
        if not query:
            return _tool_error("Query cannot be empty")
        limit = max(1, min(int(limit), MAX_LIMIT))
        client = _client_from_instance(record)
        results = client.search_read(
            "crm.lead",
            ["|", "|", ["name", "ilike", query], ["contact_name", "ilike", query], ["email_from", "ilike", query]],
            ["id", "name", "contact_name", "email_from", "phone", "stage_id"],
            limit=limit,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_create_lead(
    instance: str,
    name: str,
    contact_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    try:
        record = _require_active_instance(instance)
        if not (name or "").strip():
            return _tool_error("Lead name cannot be empty")
        client = _client_from_instance(record)
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
def odoo_search_sale_orders(instance: str, query: str, limit: int = 10) -> dict[str, Any]:
    try:
        record = _require_active_instance(instance)
        query = (query or "").strip()
        if not query:
            return _tool_error("Query cannot be empty")
        limit = max(1, min(int(limit), MAX_LIMIT))
        client = _client_from_instance(record)
        results = client.search_read(
            "sale.order",
            ["|", ["name", "ilike", query], ["client_order_ref", "ilike", query]],
            ["id", "name", "partner_id", "amount_total", "state", "date_order"],
            limit=limit,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))

