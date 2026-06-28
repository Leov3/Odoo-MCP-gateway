from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any
from urllib.parse import urljoin

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


def _validate_limit(limit: int | float | str, default: int = 10) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_LIMIT))


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_purchase_intent(query: str) -> str:
    normalized = _normalize_text(query)
    if not normalized:
        return ""

    prefixes = (
        "quiero comprar",
        "quiero adquirir",
        "quiero un",
        "quiero una",
        "quiero unos",
        "quiero unas",
        "quiero",
        "necesito",
        "necesitamos",
        "busco",
        "busca",
        "buscando",
        "requiero",
        "me hace falta",
    )
    for _ in range(3):
        removed = False
        for prefix in prefixes:
            if normalized.startswith(f"{prefix} "):
                normalized = normalized[len(prefix) :].strip()
                removed = True
        if not removed:
            break
    return normalized


def _singularize_token(token: str) -> str:
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


PRODUCT_QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "abrelatas": ("abridor de latas", "abridor de lata", "can opener"),
}


def _expand_product_queries(query: str) -> list[str]:
    base_query = _strip_purchase_intent(query)
    if not base_query:
        return []

    variants = {base_query}
    singularized = " ".join(_singularize_token(token) for token in base_query.split())
    if singularized:
        variants.add(singularized)

    for synonym in PRODUCT_QUERY_SYNONYMS.get(base_query, ()):
        variants.add(_normalize_text(synonym))

    return [variant for variant in variants if variant]


def _or_domain(conditions: list[tuple[str, str, Any]]) -> list[Any]:
    if not conditions:
        return []
    if len(conditions) == 1:
        return [conditions[0]]
    return ["|"] * (len(conditions) - 1) + conditions


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1])
    return str(value or "")


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


def _public_product_url(instance: dict[str, Any], record: dict[str, Any]) -> str | None:
    website_url = record.get("website_url")
    if not website_url:
        return None
    url = str(website_url).strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    base_url = str(instance.get("url") or "").strip()
    if not base_url:
        return url
    return urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))


def _attach_public_url(instance: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    public_url = _public_product_url(instance, record)
    if public_url:
        record = dict(record)
        record["public_url"] = public_url
    return record


def _record_text(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("name", "display_name", "complete_name", "default_code", "barcode", "description_sale"):
        value = record.get(key)
        if value:
            parts.append(str(value))

    for key in ("categ_id", "product_tmpl_id", "partner_id", "user_id", "stage_id"):
        value = record.get(key)
        if value:
            parts.append(_m2o_name(value))

    return _normalize_text(" ".join(parts))


def _relevance_score(query_variants: list[str], record: dict[str, Any], *, stock_boost: float = 0.0) -> float:
    if not query_variants:
        score = float(record.get("free_qty") or record.get("qty_available") or 0.0)
        return score + stock_boost

    candidate = _record_text(record)
    score = 0.0
    for raw_query in query_variants:
        query = _normalize_text(raw_query)
        if not query:
            continue

        tokens = query.split()
        if query == candidate:
            score += 300.0
        if query in candidate:
            score += 150.0
        if tokens and all(token in candidate for token in tokens):
            score += 60.0

        name = _normalize_text(record.get("name"))
        display_name = _normalize_text(record.get("display_name"))
        default_code = _normalize_text(record.get("default_code"))
        barcode = _normalize_text(record.get("barcode"))
        description_sale = _normalize_text(record.get("description_sale"))
        category_name = _normalize_text(_m2o_name(record.get("categ_id")))
        template_name = _normalize_text(_m2o_name(record.get("product_tmpl_id")))

        if query == name:
            score += 120.0
        elif query and query in name:
            score += 80.0

        if query == display_name:
            score += 110.0
        elif query and query in display_name:
            score += 70.0

        if query == default_code:
            score += 250.0
        elif query and query in default_code:
            score += 140.0

        if query == barcode:
            score += 260.0
        elif query and query in barcode:
            score += 150.0

        if query and query in description_sale:
            score += 30.0
        if query and query in category_name:
            score += 35.0
        if query and query in template_name:
            score += 20.0

    if record.get("category_match"):
        score += 45.0
    if record.get("stock_match"):
        score += 20.0

    stock_value = record.get("free_qty")
    if not isinstance(stock_value, (int, float)):
        stock_value = record.get("qty_available")
    if isinstance(stock_value, (int, float)):
        score += min(float(stock_value), 100.0) * 0.2

    score += stock_boost
    return score


def _merge_product_candidates(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        key = (str(item.get("record_model")), int(item.get("id")))
        current = merged.get(key)
        if current is None or float(item.get("relevance", 0.0)) > float(current.get("relevance", 0.0)):
            merged[key] = item
        else:
            if item.get("category_match"):
                current["category_match"] = True
            if item.get("stock_match"):
                current["stock_match"] = True

    ordered = sorted(
        merged.values(),
        key=lambda item: (
            -float(item.get("relevance", 0.0)),
            str(item.get("display_name") or item.get("name") or ""),
            int(item.get("id") or 0),
        ),
    )
    return ordered[:limit]


def _product_template_fields() -> list[str]:
    return [
        "id",
        "name",
        "display_name",
        "default_code",
        "barcode",
        "description_sale",
        "website_url",
        "categ_id",
        "list_price",
        "currency_id",
        "sale_ok",
        "purchase_ok",
        "type",
        "qty_available",
        "virtual_available",
        "incoming_qty",
        "outgoing_qty",
        "product_variant_count",
        "product_variant_id",
    ]


def _product_variant_fields() -> list[str]:
    return [
        "id",
        "name",
        "display_name",
        "default_code",
        "barcode",
        "product_tmpl_id",
        "lst_price",
        "website_url",
        "qty_available",
        "free_qty",
        "virtual_available",
        "incoming_qty",
        "outgoing_qty",
    ]


def _category_fields() -> list[str]:
    return ["id", "name", "complete_name", "parent_id", "product_count"]


def _product_domain_for_query(query: str, *, model: str) -> list[Any]:
    conditions = [
        ("name", "ilike", query),
        ("default_code", "ilike", query),
        ("barcode", "ilike", query),
    ]
    if model == "product.template":
        conditions.insert(3, ("description_sale", "ilike", query))
    return _or_domain(conditions)


def _search_product_records(
    client: OdooClient,
    query: str,
    *,
    model: str,
    fields: list[str],
    extra_domain: list[Any] | None = None,
    limit: int = MAX_LIMIT,
) -> list[dict[str, Any]]:
    query_variants = _expand_product_queries(query)
    if query and not query_variants and not extra_domain:
        return []
    domains: list[list[Any]] = []

    if query_variants:
        for variant in query_variants:
            domains.append(_product_domain_for_query(variant, model=model))
    else:
        domains.append([])

    if extra_domain:
        if domains == [[]]:
            domains = [list(extra_domain)]
        else:
            domains = [list(extra_domain) + domain for domain in domains]

    records: list[dict[str, Any]] = []
    for domain in domains:
        results = client.search_read(model, domain, fields, limit=MAX_LIMIT)
        for result in results:
            item = dict(result)
            item["record_model"] = model
            item["relevance"] = _relevance_score(query_variants, item)
            records.append(item)

    return _merge_product_candidates(records, limit)


def _search_category_matches(client: OdooClient, query: str) -> list[dict[str, Any]]:
    query_variants = _expand_product_queries(query)
    if not query_variants:
        return []

    records: list[dict[str, Any]] = []
    for variant in query_variants:
        domain = _or_domain(
            [
                ("name", "ilike", variant),
                ("complete_name", "ilike", variant),
            ]
        )
        matches = client.search_read(
            "product.category",
            domain,
            _category_fields(),
            limit=MAX_LIMIT,
        )
        records.extend(matches)
    return _merge_product_candidates(
        [
            {
                **record,
                "record_model": "product.category",
                "relevance": _relevance_score(query_variants, record),
            }
            for record in records
        ],
        MAX_LIMIT,
    )


def _read_single_record(client: OdooClient, model: str, record_id: int, fields: list[str]) -> dict[str, Any]:
    records = client.read(model, [record_id], fields)
    if not records:
        raise ValueError(f'{model} record "{record_id}" was not found')
    return dict(records[0])


def _normalize_product_model(model: str) -> str:
    normalized = (model or "").strip()
    if normalized not in {"product.template", "product.product"}:
        raise ValueError('product_model must be "product.template" or "product.product"')
    return normalized


def _product_detail_fields(model: str) -> list[str]:
    if model == "product.product":
        return [
            "id",
            "name",
            "display_name",
            "default_code",
            "barcode",
            "product_tmpl_id",
            "lst_price",
            "qty_available",
            "free_qty",
            "virtual_available",
            "incoming_qty",
            "outgoing_qty",
        ]
    return _product_template_fields()


def _stock_fields(model: str) -> list[str]:
    if model == "product.product":
        return [
            "id",
            "display_name",
            "default_code",
            "barcode",
            "product_tmpl_id",
            "qty_available",
            "free_qty",
            "virtual_available",
            "incoming_qty",
            "outgoing_qty",
        ]
    return [
        "id",
        "display_name",
        "default_code",
        "barcode",
        "categ_id",
        "qty_available",
        "virtual_available",
        "incoming_qty",
        "outgoing_qty",
        "product_variant_count",
    ]


def _resolve_variant_id(client: OdooClient, product_model: str, product_id: int) -> int:
    if product_model == "product.product":
        return product_id

    record = _read_single_record(
        client,
        "product.template",
        product_id,
        ["id", "product_variant_count", "product_variant_id", "display_name"],
    )
    variant_id = _m2o_id(record.get("product_variant_id"))
    if record.get("product_variant_count") and int(record["product_variant_count"]) > 1:
        raise ValueError(
            "Selected product template has multiple variants; choose a specific product.product record"
        )
    if variant_id is None:
        raise ValueError(f'Product template "{product_id}" does not have a variant')
    return variant_id


def _sale_order_fields() -> list[str]:
    return [
        "id",
        "name",
        "partner_id",
        "partner_invoice_id",
        "partner_shipping_id",
        "pricelist_id",
        "state",
        "date_order",
        "validity_date",
        "amount_untaxed",
        "amount_tax",
        "amount_total",
        "currency_id",
        "user_id",
        "team_id",
        "note",
        "client_order_ref",
        "activity_state",
    ]


def _sale_order_line_fields() -> list[str]:
    return [
        "id",
        "order_id",
        "product_id",
        "product_template_id",
        "name",
        "product_uom_qty",
        "qty_delivered",
        "qty_invoiced",
        "price_unit",
        "discount",
        "price_subtotal",
        "price_total",
        "tax_id",
        "currency_id",
    ]


def _partner_fields() -> list[str]:
    return [
        "id",
        "name",
        "display_name",
        "company_type",
        "email",
        "phone",
        "mobile",
        "vat",
        "customer_rank",
        "supplier_rank",
        "street",
        "city",
        "zip",
        "country_id",
        "state_id",
        "parent_id",
        "user_id",
        "category_id",
        "child_ids",
        "active",
    ]


def _normalize_partner_name(name: str | None) -> str:
    normalized = (name or "").strip()
    if not normalized:
        raise ValueError("Partner name cannot be empty")
    return normalized


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
        limit_value = _validate_limit(limit)
        client = _client_from_instance(instance)
        results = client.search_read(
            "res.partner",
            ["|", "|", "|", ["name", "ilike", query_value], ["email", "ilike", query_value], ["phone", "ilike", query_value], ["mobile", "ilike", query_value]],
            ["id", "name", "display_name", "email", "phone", "mobile", "company_type", "customer_rank"],
            limit=limit_value,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_create_partner(
    name: str,
    email: str | None = None,
    phone: str | None = None,
    mobile: str | None = None,
    street: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    vat: str | None = None,
    company_type: str | None = None,
    category_ids: list[int] | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        values: dict[str, Any] = {
            "name": _normalize_partner_name(name),
            "customer_rank": 1,
        }
        if email:
            values["email"] = email.strip()
        if phone:
            values["phone"] = phone.strip()
        if mobile:
            values["mobile"] = mobile.strip()
        if street:
            values["street"] = street.strip()
        if city:
            values["city"] = city.strip()
        if zip_code:
            values["zip"] = zip_code.strip()
        if vat:
            values["vat"] = vat.strip()
        if company_type in {"person", "company"}:
            values["company_type"] = company_type
        if category_ids is not None:
            values["category_id"] = [(6, 0, [int(category_id) for category_id in category_ids])]

        partner_id = client.create("res.partner", values)
        partner = _read_single_record(client, "res.partner", partner_id, _partner_fields())
        return {"success": True, "created": True, "partner_id": partner_id, "record": partner}
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
        limit_value = _validate_limit(limit)
        client = _client_from_instance(instance)
        results = client.search_read(
            "crm.lead",
            ["|", "|", "|", "|", ["name", "ilike", query_value], ["contact_name", "ilike", query_value], ["email_from", "ilike", query_value], ["partner_name", "ilike", query_value], ["phone", "ilike", query_value]],
            ["id", "name", "contact_name", "partner_name", "email_from", "phone", "stage_id", "probability", "expected_revenue", "user_id", "activity_state"],
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
        limit_value = _validate_limit(limit)
        client = _client_from_instance(instance)
        results = client.search_read(
            "sale.order",
            ["|", ["name", "ilike", query_value], ["client_order_ref", "ilike", query_value]],
            ["id", "name", "partner_id", "client_order_ref", "amount_total", "state", "date_order", "activity_state", "currency_id"],
            limit=limit_value,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_products(
    query: str,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        cleaned_query = _strip_purchase_intent(query)
        if not cleaned_query:
            return _tool_error("Query cannot be empty")
        limit_value = _validate_limit(limit)
        client = _client_from_instance(instance)
        category_matches = _search_category_matches(client, query)
        category_ids = [record["id"] for record in category_matches]

        template_results = _search_product_records(
            client,
            query,
            model="product.template",
            fields=_product_template_fields(),
            extra_domain=[("categ_id", "in", category_ids)] if category_ids else None,
            limit=limit_value,
        )
        variant_results = _search_product_records(
            client,
            query,
            model="product.product",
            fields=_product_variant_fields(),
            limit=limit_value,
        )

        results = _merge_product_candidates(
            [
                *template_results,
                *variant_results,
                *[
                    {
                        **record,
                        "record_model": "product.template",
                        "category_match": True,
                        "relevance": _relevance_score(_expand_product_queries(query), record) + 30.0,
                    }
                    for record in template_results
                    if category_ids and record.get("categ_id") and _m2o_id(record.get("categ_id")) in category_ids
                ],
            ],
            limit_value,
        )
        results = [_attach_public_url(instance, record) for record in results]

        return {
            "success": True,
            "count": len(results),
            "matched_categories": category_matches,
            "results": results,
        }
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_products_by_sku(
    query: str,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        cleaned_query = _strip_purchase_intent(query)
        if not cleaned_query:
            return _tool_error("Query cannot be empty")
        client = _client_from_instance(instance)
        limit_value = _validate_limit(limit)
        results = _merge_product_candidates(
            [
                *_search_product_records(
                    client,
                    cleaned_query,
                    model="product.template",
                    fields=_product_template_fields(),
                    limit=limit_value,
                ),
                *_search_product_records(
                    client,
                    cleaned_query,
                    model="product.product",
                    fields=_product_variant_fields(),
                    limit=limit_value,
                ),
            ],
            limit_value,
        )
        results = [_attach_public_url(instance, record) for record in results]
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_products_by_category(
    query: str,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        cleaned_query = _strip_purchase_intent(query)
        if not cleaned_query:
            return _tool_error("Query cannot be empty")
        client = _client_from_instance(instance)
        limit_value = _validate_limit(limit)
        categories = _search_category_matches(client, cleaned_query)
        category_ids = [record["id"] for record in categories]
        results = _search_product_records(
            client,
            cleaned_query,
            model="product.template",
            fields=_product_template_fields(),
            extra_domain=[("categ_id", "in", category_ids)] if category_ids else None,
            limit=limit_value,
        )
        results = [_attach_public_url(instance, record) for record in results]
        return {
            "success": True,
            "count": len(results),
            "matched_categories": categories,
            "results": results,
        }
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_product_variants(
    query: str,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        cleaned_query = _strip_purchase_intent(query)
        if not cleaned_query:
            return _tool_error("Query cannot be empty")
        client = _client_from_instance(instance)
        limit_value = _validate_limit(limit)
        results = _search_product_records(
            client,
            cleaned_query,
            model="product.product",
            fields=_product_variant_fields(),
            limit=limit_value,
        )
        results = [_attach_public_url(instance, record) for record in results]
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_products_by_stock(
    query: str = "",
    min_qty: float = 1.0,
    limit: int = 10,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        limit_value = _validate_limit(limit)
        min_qty_value = max(0.0, float(min_qty))
        cleaned_query = _strip_purchase_intent(query) if query else ""
        template_stock_field = "qty_available"
        variant_stock_field = "free_qty"
        results = _merge_product_candidates(
            [
                *[
                    {
                        **record,
                        "stock_match": True,
                    }
                    for record in _search_product_records(
                        client,
                        cleaned_query,
                        model="product.template",
                        fields=_product_template_fields(),
                        extra_domain=[(template_stock_field, ">=", min_qty_value)],
                        limit=limit_value,
                    )
                ],
                *[
                    {
                        **record,
                        "stock_match": True,
                    }
                    for record in _search_product_records(
                        client,
                        cleaned_query,
                        model="product.product",
                        fields=_product_variant_fields(),
                        extra_domain=[(variant_stock_field, ">=", min_qty_value)],
                        limit=limit_value,
                    )
                ],
            ],
            limit_value,
        )
        results = [_attach_public_url(instance, record) for record in results]
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_get_partner(
    partner_id: int,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        partner = _read_single_record(client, "res.partner", int(partner_id), _partner_fields())
        return {"success": True, "record": partner}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_update_partner(
    partner_id: int,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    mobile: str | None = None,
    street: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    vat: str | None = None,
    category_ids: list[int] | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        values: dict[str, Any] = {}
        for key, value in (
            ("name", name),
            ("email", email),
            ("phone", phone),
            ("mobile", mobile),
            ("street", street),
            ("city", city),
            ("zip", zip_code),
            ("vat", vat),
        ):
            if value is not None and str(value).strip():
                values[key] = str(value).strip()
        if category_ids is not None:
            values["category_id"] = [(6, 0, [int(category_id) for category_id in category_ids])]
        if not values:
            return _tool_error("At least one field must be provided for update")
        updated = client.write("res.partner", [int(partner_id)], values)
        partner = _read_single_record(client, "res.partner", int(partner_id), _partner_fields())
        return {"success": updated, "record": partner}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_get_product(
    product_id: int,
    product_model: str = "product.template",
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        model = _normalize_product_model(product_model)
        product = _read_single_record(client, model, int(product_id), _product_detail_fields(model))
        return {"success": True, "record_model": model, "record": _attach_public_url(instance, product)}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_get_product_pricing(
    product_id: int,
    product_model: str = "product.template",
    partner_id: int | None = None,
    pricelist_id: int | None = None,
    quantity: float = 1.0,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        model = _normalize_product_model(product_model)
        product = _read_single_record(client, model, int(product_id), _product_detail_fields(model))
        pricing: dict[str, Any] = {
            "quantity": float(quantity),
            "reference_price": product.get("list_price") if model == "product.template" else product.get("lst_price"),
            "currency_id": product.get("currency_id"),
            "note": "Computed pricelist pricing is not available yet; this returns the stored sale price.",
        }
        if partner_id is not None:
            pricing["partner"] = _read_single_record(
                client,
                "res.partner",
                int(partner_id),
                ["id", "name", "display_name", "property_product_pricelist", "property_payment_term_id"],
            )
        if pricelist_id is not None:
            pricing["pricelist"] = _read_single_record(
                client,
                "product.pricelist",
                int(pricelist_id),
                ["id", "name", "currency_id", "discount_policy"],
            )
        return {"success": True, "record_model": model, "record": _attach_public_url(instance, product), "pricing": pricing}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_get_stock_availability(
    product_id: int,
    product_model: str = "product.template",
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        model = _normalize_product_model(product_model)
        stock = _read_single_record(client, model, int(product_id), _stock_fields(model))
        return {"success": True, "record_model": model, "record": _attach_public_url(instance, stock)}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_get_sale_order(
    order_id: int,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        order = _read_single_record(client, "sale.order", int(order_id), _sale_order_fields())
        lines = client.search_read(
            "sale.order.line",
            [("order_id", "=", int(order_id))],
            _sale_order_line_fields(),
            limit=MAX_LIMIT,
        )
        return {"success": True, "order": order, "lines": lines, "line_count": len(lines)}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_create_quotation(
    partner_id: int,
    client_order_ref: str | None = None,
    validity_date: str | None = None,
    note: str | None = None,
    pricelist_id: int | None = None,
    user_id: int | None = None,
    team_id: int | None = None,
    payment_term_id: int | None = None,
    partner_invoice_id: int | None = None,
    partner_shipping_id: int | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        values: dict[str, Any] = {"partner_id": int(partner_id)}
        if client_order_ref:
            values["client_order_ref"] = client_order_ref.strip()
        if validity_date:
            values["validity_date"] = validity_date.strip()
        if note:
            values["note"] = note.strip()
        if pricelist_id is not None:
            values["pricelist_id"] = int(pricelist_id)
        if user_id is not None:
            values["user_id"] = int(user_id)
        if team_id is not None:
            values["team_id"] = int(team_id)
        if payment_term_id is not None:
            values["payment_term_id"] = int(payment_term_id)
        if partner_invoice_id is not None:
            values["partner_invoice_id"] = int(partner_invoice_id)
        if partner_shipping_id is not None:
            values["partner_shipping_id"] = int(partner_shipping_id)
        order_id = client.create("sale.order", values)
        order = _read_single_record(client, "sale.order", order_id, _sale_order_fields())
        return {"success": True, "created": True, "order_id": order_id, "order": order}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_add_quotation_line(
    order_id: int,
    product_id: int,
    product_model: str = "product.product",
    quantity: float = 1.0,
    price_unit: float | None = None,
    discount: float | None = None,
    name: str | None = None,
    uom_id: int | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        model = _normalize_product_model(product_model)
        resolved_product_id = _resolve_variant_id(client, model, int(product_id))
        values: dict[str, Any] = {
            "order_id": int(order_id),
            "product_id": resolved_product_id,
            "product_uom_qty": float(quantity),
        }
        if price_unit is not None:
            values["price_unit"] = float(price_unit)
        if discount is not None:
            values["discount"] = float(discount)
        if name:
            values["name"] = name.strip()
        if uom_id is not None:
            values["product_uom"] = int(uom_id)
        line_id = client.create("sale.order.line", values)
        line = _read_single_record(client, "sale.order.line", line_id, _sale_order_line_fields())
        return {"success": True, "created": True, "line_id": line_id, "record": line}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_update_quotation_line(
    line_id: int,
    quantity: float | None = None,
    price_unit: float | None = None,
    discount: float | None = None,
    name: str | None = None,
    product_model: str | None = None,
    product_id: int | None = None,
    uom_id: int | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        values: dict[str, Any] = {}
        if quantity is not None:
            values["product_uom_qty"] = float(quantity)
        if price_unit is not None:
            values["price_unit"] = float(price_unit)
        if discount is not None:
            values["discount"] = float(discount)
        if name is not None:
            values["name"] = name.strip()
        if product_model is not None and product_id is not None:
            resolved_model = _normalize_product_model(product_model)
            values["product_id"] = _resolve_variant_id(client, resolved_model, int(product_id))
        if uom_id is not None:
            values["product_uom"] = int(uom_id)
        if not values:
            return _tool_error("At least one field must be provided for update")
        updated = client.write("sale.order.line", [int(line_id)], values)
        line = _read_single_record(client, "sale.order.line", int(line_id), _sale_order_line_fields())
        return {"success": updated, "record": line}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_confirm_quotation(
    order_id: int,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        result = client.call_method("sale.order", "action_confirm", ids=[int(order_id)])
        order = _read_single_record(client, "sale.order", int(order_id), _sale_order_fields())
        return {"success": True, "result": result, "order": order}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_send_quotation(
    order_id: int,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        result = client.call_method("sale.order", "action_quotation_send", ids=[int(order_id)])
        order = _read_single_record(client, "sale.order", int(order_id), _sale_order_fields())
        return {
            "success": True,
            "message": "Quotation send action prepared",
            "result": result,
            "order": order,
        }
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_cancel_sale_order(
    order_id: int,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        result = client.call_method("sale.order", "action_cancel", ids=[int(order_id)])
        order = _read_single_record(client, "sale.order", int(order_id), _sale_order_fields())
        return {"success": True, "result": result, "order": order}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_update_lead_stage(
    lead_id: int,
    stage_id: int,
    probability: float | None = None,
    user_id: int | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        values: dict[str, Any] = {"stage_id": int(stage_id)}
        if probability is not None:
            values["probability"] = float(probability)
        if user_id is not None:
            values["user_id"] = int(user_id)
        updated = client.write("crm.lead", [int(lead_id)], values)
        lead = _read_single_record(
            client,
            "crm.lead",
            int(lead_id),
            ["id", "name", "contact_name", "partner_name", "stage_id", "probability", "expected_revenue", "user_id", "activity_state"],
        )
        return {"success": updated, "record": lead}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_set_next_activity(
    record_model: str,
    record_id: int,
    activity_type_id: int,
    summary: str,
    deadline: str | None = None,
    note: str | None = None,
    user_id: int | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        values: dict[str, Any] = {
            "activity_type_id": int(activity_type_id),
            "summary": summary.strip(),
        }
        if deadline:
            values["date_deadline"] = deadline.strip()
        if note:
            values["note"] = note.strip()
        if user_id is not None:
            values["user_id"] = int(user_id)
        result = client.call_method(
            record_model,
            "activity_schedule",
            ids=[int(record_id)],
            kwargs=values,
        )
        return {"success": True, "result": result}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_log_note(
    record_model: str,
    record_id: int,
    body: str,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        result = client.call_method(
            record_model,
            "message_post",
            ids=[int(record_id)],
            kwargs={"body": body.strip(), "subtype_xmlid": "mail.mt_note"},
        )
        return {"success": True, "result": result}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_pipeline(
    query: str = "",
    limit: int = 10,
    mine_only: bool = True,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        limit_value = _validate_limit(limit)
        domain: list[Any] = [("type", "=", "opportunity")]
        if mine_only:
            uid = client.uid or client.authenticate()
            domain.append(("user_id", "=", uid))
        query_value = (query or "").strip()
        if query_value:
            domain = [
                *domain,
                *_or_domain(
                    [
                        ("name", "ilike", query_value),
                        ("contact_name", "ilike", query_value),
                        ("partner_name", "ilike", query_value),
                        ("email_from", "ilike", query_value),
                        ("phone", "ilike", query_value),
                    ]
                ),
            ]
        results = client.search_read(
            "crm.lead",
            domain,
            ["id", "name", "contact_name", "partner_name", "stage_id", "probability", "expected_revenue", "user_id", "team_id", "activity_state"],
            limit=limit_value,
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))


@mcp.tool
def odoo_search_my_activities(
    limit: int = 10,
    overdue_only: bool = False,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        uid = client.uid or client.authenticate()
        domain: list[Any] = [("user_id", "=", uid)]
        if overdue_only:
            domain.append(("date_deadline", "<", date.today().isoformat()))
        results = client.search_read(
            "mail.activity",
            domain,
            ["id", "activity_type_id", "summary", "note", "date_deadline", "res_model", "res_id", "res_name", "user_id"],
            limit=_validate_limit(limit),
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        return _tool_error(str(exc))
