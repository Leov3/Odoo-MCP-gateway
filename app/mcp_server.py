from __future__ import annotations

import logging
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
AUDIT_LOGGER = logging.getLogger("mario.audit")

CLEAR_CONFIRMATION_NORMALIZED = {
    "confirmo",
    "si quiero comprar",
    "si adelante",
    "si apruebo",
    "apruebo la cotizacion",
    "apruebo cotizacion",
    "autorizo",
    "dale",
    "confirmar",
    "confirmado",
}


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


def _public_category_fields() -> list[str]:
    return ["id", "name", "parent_id", "sequence", "website_id"]


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


def _search_public_category_matches(client: OdooClient, query: str) -> list[dict[str, Any]]:
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
            "product.public.category",
            domain,
            _public_category_fields(),
            limit=MAX_LIMIT,
        )
        records.extend(matches)
    return _merge_product_candidates(
        [
            {
                **record,
                "record_model": "product.public.category",
                "relevance": _relevance_score(query_variants, record),
            }
            for record in records
        ],
        MAX_LIMIT,
    )


def _search_all_category_matches(client: OdooClient, query: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "public_categories": _search_public_category_matches(client, query),
        "internal_categories": _search_category_matches(client, query),
    }


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
        "origin",
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


def _instance_slug(instance: dict[str, Any]) -> str:
    return str(instance.get("slug") or instance.get("name") or "instance")


def _audit_event(
    event: str,
    *,
    instance: dict[str, Any],
    phone: str | None = None,
    partner_id: int | None = None,
    order_id: int | None = None,
    payload: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    log_method = getattr(AUDIT_LOGGER, level, AUDIT_LOGGER.info)
    log_method(
        "%s",
        {
            "event": event,
            "instance": _instance_slug(instance),
            "phone": phone,
            "partner_id": partner_id,
            "order_id": order_id,
            "payload": payload or {},
        },
    )


def _normalize_phone(phone: str | None) -> str:
    return re.sub(r"\D+", "", str(phone or ""))


def _normalize_whatsapp_confirmation(text: str | None) -> str:
    value = (text or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _is_clear_confirmation(text: str | None) -> bool:
    normalized = _normalize_whatsapp_confirmation(text)
    if not normalized:
        return False
    if normalized in CLEAR_CONFIRMATION_NORMALIZED:
        return True
    prefixes = (
        "confirmo",
        "si quiero comprar",
        "si adelante",
        "apruebo",
        "autorizo",
        "dale",
    )
    return any(normalized.startswith(prefix) for prefix in prefixes)


def _phone_variants(phone: str) -> list[str]:
    raw = (phone or "").strip()
    digits = _normalize_phone(raw)
    variants: list[str] = []
    for candidate in (raw, digits, digits[-10:] if len(digits) > 10 else digits):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _partner_fields_for_whatsapp() -> list[str]:
    return [
        "id",
        "name",
        "display_name",
        "email",
        "phone",
        "mobile",
        "company_type",
        "customer_rank",
        "supplier_rank",
        "active",
    ]


def _read_partner(client: OdooClient, partner_id: int) -> dict[str, Any]:
    partner = _read_single_record(client, "res.partner", int(partner_id), _partner_fields())
    return partner


def _score_partner_match(phone: str, record: dict[str, Any]) -> float:
    query_digits = _normalize_phone(phone)
    score = 0.0
    for field in ("phone", "mobile"):
        value_digits = _normalize_phone(record.get(field))
        if not value_digits:
            continue
        if query_digits and value_digits == query_digits:
            score += 200.0
        elif query_digits and query_digits in value_digits:
            score += 120.0
        elif value_digits and value_digits in query_digits:
            score += 90.0
        elif record.get(field) and phone and str(record[field]).strip() == phone.strip():
            score += 180.0
    if record.get("customer_rank"):
        score += 5.0
    return score


def _search_partners_by_phone(client: OdooClient, phone: str, limit: int = 5) -> list[dict[str, Any]]:
    query_value = (phone or "").strip()
    if not query_value:
        return []

    variants = _phone_variants(query_value)
    records: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for variant in variants:
        results = client.search_read(
            "res.partner",
            ["|", ["phone", "ilike", variant], ["mobile", "ilike", variant]],
            _partner_fields_for_whatsapp(),
            limit=max(1, min(limit, MAX_LIMIT)),
        )
        for result in results:
            partner_id = int(result.get("id") or 0)
            if partner_id in seen_ids:
                continue
            seen_ids.add(partner_id)
            item = dict(result)
            item["match_score"] = _score_partner_match(query_value, item)
            records.append(item)

    records.sort(
        key=lambda record: (
            -float(record.get("match_score", 0.0)),
            str(record.get("display_name") or record.get("name") or ""),
            int(record.get("id") or 0),
        )
    )
    return records[: max(1, min(limit, MAX_LIMIT))]


def _search_partner_by_phone_best(client: OdooClient, phone: str) -> dict[str, Any] | None:
    results = _search_partners_by_phone(client, phone, limit=5)
    return results[0] if results else None


def _append_order_origin(
    values: dict[str, Any],
    *,
    whatsapp_phone: str,
    notes: str | None = None,
) -> None:
    origin_text = f"WhatsApp {whatsapp_phone}".strip()
    current_note = values.get("note") or ""
    note_parts = [part for part in (current_note.strip(), f"Origen: {origin_text}", notes.strip() if notes else "") if part]
    values["note"] = "\n".join(note_parts).strip()
    values["client_order_ref"] = values.get("client_order_ref") or origin_text
    values["origin"] = values.get("origin") or origin_text


def _normalize_quote_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_item in items:
        if raw_item.get("price_unit") is not None:
            raise ValueError("Price overrides are not allowed in the WhatsApp flow")
        if raw_item.get("discount") is not None:
            raise ValueError("Discounts are not allowed in the WhatsApp flow")
        product_id = raw_item.get("product_id")
        quantity = raw_item.get("quantity", raw_item.get("product_uom_qty", 1))
        if product_id is None:
            raise ValueError("Each item must include product_id")
        quantity_value = float(quantity)
        if quantity_value <= 0:
            raise ValueError("Quantity must be greater than zero")
        product_model = _normalize_product_model(str(raw_item.get("product_model") or "product.product"))
        item: dict[str, Any] = {
            "product_id": int(product_id),
            "product_model": product_model,
            "quantity": quantity_value,
        }
        if raw_item.get("name"):
            item["name"] = str(raw_item["name"]).strip()
        if raw_item.get("price_unit") is not None:
            item["price_unit"] = float(raw_item["price_unit"])
        if raw_item.get("discount") is not None:
            item["discount"] = float(raw_item["discount"])
        if raw_item.get("uom_id") is not None:
            item["uom_id"] = int(raw_item["uom_id"])
        normalized.append(item)
    return normalized


def _read_order_with_lines(client: OdooClient, order_id: int) -> dict[str, Any]:
    order = _read_single_record(client, "sale.order", int(order_id), _sale_order_fields())
    lines = client.search_read(
        "sale.order.line",
        [("order_id", "=", int(order_id))],
        _sale_order_line_fields(),
        limit=MAX_LIMIT,
    )
    return {"order": order, "lines": lines, "line_count": len(lines)}


def _find_activity_type_id(client: OdooClient) -> int | None:
    candidates = client.search_read(
        "mail.activity.type",
        ["|", ["name", "ilike", "todo"], ["name", "ilike", "call"]],
        ["id", "name"],
        limit=5,
    )
    if candidates:
        return int(candidates[0]["id"])
    fallback = client.search_read("mail.activity.type", [], ["id", "name"], limit=1)
    if fallback:
        return int(fallback[0]["id"])
    return None


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
            ["id", "name", "partner_id", "client_order_ref", "origin", "amount_total", "state", "date_order", "activity_state", "currency_id"],
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
        category_matches = _search_all_category_matches(client, cleaned_query)
        public_category_ids = [record["id"] for record in category_matches["public_categories"]]
        internal_category_ids = [record["id"] for record in category_matches["internal_categories"]]
        category_ids = public_category_ids or internal_category_ids
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
            "matched_public_categories": category_matches["public_categories"],
            "matched_internal_categories": category_matches["internal_categories"],
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
    origin: str | None = None,
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
        if origin:
            values["origin"] = origin.strip()
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


def _normalize_tracking_text(value: Any) -> str:
    return str(value or "").strip()


def _partner_tracking_fields() -> list[str]:
    return [
        "id",
        "name",
        "display_name",
        "email",
        "phone",
        "mobile",
        "street",
        "city",
        "zip",
        "country_id",
        "state_id",
        "vat",
        "active",
    ]


def _tracking_missing_fields(partner: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not _normalize_tracking_text(partner.get("name")):
        missing.append("nombre_contacto")
    if not _normalize_tracking_text(partner.get("email")):
        missing.append("correo")
    if not _normalize_tracking_text(partner.get("street")):
        missing.append("direccion")
    if not _normalize_tracking_text(partner.get("city")):
        missing.append("ciudad")
    if not _normalize_tracking_text(partner.get("zip")):
        missing.append("codigo_postal")
    return missing


def _tracking_customer_message(missing_fields: list[str]) -> str:
    labels = {
        "nombre_contacto": "el nombre del contacto",
        "correo": "el correo electrónico",
        "direccion": "la dirección de entrega",
        "ciudad": "la ciudad",
        "codigo_postal": "el código postal",
    }
    ordered = [labels[field] for field in missing_fields if field in labels]
    if not ordered:
        return "Necesito datos de seguimiento antes de confirmar."
    if len(ordered) == 1:
        return f"Para continuar necesito {ordered[0]}."
    if len(ordered) == 2:
        return f"Para continuar necesito {ordered[0]} y {ordered[1]}."
    return "Para continuar necesito " + ", ".join(ordered[:-1]) + f" y {ordered[-1]}."


@mcp.tool
def validar_y_preparar_confirmacion_whatsapp(
    order_id: int,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        order = _read_single_record(client, "sale.order", int(order_id), _sale_order_fields())
        partner_id = _m2o_id(order.get("partner_id"))
        shipping_id = _m2o_id(order.get("partner_shipping_id"))
        contact_id = shipping_id or partner_id
        if contact_id is None:
            return _tool_error("Sale order has no partner linked", order=order)

        partner = _read_single_record(client, "res.partner", int(contact_id), _partner_tracking_fields())
        missing_fields = _tracking_missing_fields(partner)
        if missing_fields:
            message = _tracking_customer_message(missing_fields)
            note = (
                f"[WhatsApp] Confirmación bloqueada para la orden {order.get('name')}.\n"
                f"Faltan datos de seguimiento: {', '.join(missing_fields)}.\n"
                f"Acción requerida: solicitar al cliente {message}"
            )
            try:
                client.call_method(
                    "sale.order",
                    "message_post",
                    ids=[int(order_id)],
                    kwargs={"body": note, "subtype_xmlid": "mail.mt_note"},
                )
            except Exception:
                pass
            _audit_event(
                "whatsapp_confirmation_blocked",
                instance=instance,
                partner_id=partner_id,
                order_id=int(order_id),
                payload={"missing_fields": missing_fields, "contact_id": contact_id},
            )
            return {
                "success": True,
                "can_confirm": False,
                "order_id": int(order_id),
                "order": order,
                "contact": partner,
                "missing_fields": missing_fields,
                "message_for_customer": message,
                "note_written": True,
            }

        ready_note = (
            f"[WhatsApp] Pedido listo para confirmación.\n"
            f"Cliente: {partner.get('display_name') or partner.get('name') or contact_id}.\n"
            f"Orden: {order.get('name')}.\n"
            f"Datos de seguimiento completos."
        )
        try:
            client.call_method(
                "sale.order",
                "message_post",
                ids=[int(order_id)],
                kwargs={"body": ready_note, "subtype_xmlid": "mail.mt_note"},
            )
        except Exception:
            pass
        _audit_event(
            "whatsapp_confirmation_ready",
            instance=instance,
            partner_id=partner_id,
            order_id=int(order_id),
            payload={"contact_id": contact_id},
        )
        return {
            "success": True,
            "can_confirm": True,
            "order_id": int(order_id),
            "order": order,
            "contact": partner,
            "missing_fields": [],
            "message_for_customer": "Listo, ya puedo confirmar tu pedido.",
            "note_written": True,
        }
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


def _partner_payload_from_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    payload.pop("match_score", None)
    return payload


def _result_product_payload(instance: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    payload = _attach_public_url(instance, record)
    return payload


def _product_stock_value(record: dict[str, Any]) -> float | None:
    for key in ("free_qty", "qty_available", "virtual_available"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


@mcp.tool
def buscar_cliente_por_telefono(
    phone: str,
    limit: int = 5,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        query_value = (phone or "").strip()
        if not query_value:
            return _tool_error("Phone cannot be empty")
        limit_value = _validate_limit(limit)
        client = _client_from_instance(instance)
        results = _search_partners_by_phone(client, query_value, limit=limit_value)
        best_match = _partner_payload_from_record(results[0]) if results else None
        _audit_event(
            "whatsapp_partner_lookup",
            instance=instance,
            phone=query_value,
            partner_id=int(best_match["id"]) if best_match else None,
            payload={"count": len(results)},
        )
        return {
            "success": True,
            "count": len(results),
            "best_match": best_match,
            "results": [_partner_payload_from_record(record) for record in results],
        }
    except Exception as exc:
        _audit_event("whatsapp_partner_lookup_error", instance=_instance_from_request(request), phone=phone, payload={"error": str(exc)}, level="error")
        return _tool_error(str(exc))


@mcp.tool
def crear_o_actualizar_cliente_whatsapp(
    phone: str,
    name: str | None = None,
    email: str | None = None,
    mobile: str | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        query_value = (phone or "").strip()
        if not query_value:
            return _tool_error("Phone cannot be empty")
        client = _client_from_instance(instance)
        existing = _search_partner_by_phone_best(client, query_value)
        if existing:
            partner_id = int(existing["id"])
            updates: dict[str, Any] = {}
            if email and not existing.get("email"):
                updates["email"] = email.strip()
            if mobile and not existing.get("mobile"):
                updates["mobile"] = mobile.strip()
            elif not existing.get("mobile"):
                updates["mobile"] = query_value
            if not existing.get("phone"):
                updates["phone"] = query_value
            updated = False
            if updates:
                updated = client.write("res.partner", [partner_id], updates)
            partner = _read_partner(client, partner_id)
            _audit_event(
                "whatsapp_partner_reused",
                instance=instance,
                phone=query_value,
                partner_id=partner_id,
                payload={"updated": updated, "updates": list(updates.keys())},
            )
            return {
                "success": True,
                "created": False,
                "updated": updated,
                "partner_id": partner_id,
                "record": partner,
            }

        partner_name = _normalize_partner_name(name)
        values: dict[str, Any] = {
            "name": partner_name,
            "customer_rank": 1,
            "phone": query_value,
        }
        values["mobile"] = mobile.strip() if mobile and mobile.strip() else query_value
        if email and email.strip():
            values["email"] = email.strip()
        partner_id = client.create("res.partner", values)
        partner = _read_partner(client, partner_id)
        _audit_event(
            "whatsapp_partner_created",
            instance=instance,
            phone=query_value,
            partner_id=partner_id,
            payload={"name": partner_name},
        )
        return {
            "success": True,
            "created": True,
            "updated": False,
            "partner_id": partner_id,
            "record": partner,
        }
    except Exception as exc:
        _audit_event(
            "whatsapp_partner_upsert_error",
            instance=_instance_from_request(request),
            phone=phone,
            payload={"error": str(exc)},
            level="error",
        )
        return _tool_error(str(exc))


@mcp.tool
def buscar_producto_venta(
    query: str,
    limit: int = 3,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        cleaned_query = _strip_purchase_intent(query)
        if not cleaned_query:
            return _tool_error("Query cannot be empty")
        limit_value = max(1, min(_validate_limit(limit), 3))
        client = _client_from_instance(instance)
        category_matches = _search_all_category_matches(client, cleaned_query)
        public_category_ids = [record["id"] for record in category_matches["public_categories"]]
        internal_category_ids = [record["id"] for record in category_matches["internal_categories"]]
        category_ids = public_category_ids or internal_category_ids
        template_results = _search_product_records(
            client,
            cleaned_query,
            model="product.template",
            fields=_product_template_fields(),
            extra_domain=[("categ_id", "in", category_ids)] if category_ids else None,
            limit=limit_value,
        )
        variant_results = _search_product_records(
            client,
            cleaned_query,
            model="product.product",
            fields=_product_variant_fields(),
            limit=limit_value,
        )
        results = _merge_product_candidates(
            [
                *template_results,
                *variant_results,
            ],
            limit_value,
        )
        results = [_result_product_payload(instance, record) for record in results]
        _audit_event(
            "whatsapp_product_lookup",
            instance=instance,
            payload={"query": cleaned_query, "count": len(results)},
        )
        return {
            "success": True,
            "count": len(results),
            "matched_public_categories": category_matches["public_categories"],
            "matched_internal_categories": category_matches["internal_categories"],
            "results": results,
        }
    except Exception as exc:
        _audit_event(
            "whatsapp_product_lookup_error",
            instance=_instance_from_request(request),
            payload={"error": str(exc), "query": query},
            level="error",
        )
        return _tool_error(str(exc))


@mcp.tool
def consultar_disponibilidad(
    product_id: int,
    product_model: str = "product.template",
    quantity: float = 1.0,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        model = _normalize_product_model(product_model)
        record = _read_single_record(client, model, int(product_id), _stock_fields(model))
        available_qty = _product_stock_value(record)
        requested_qty = float(quantity)
        can_fulfill = available_qty is not None and available_qty >= requested_qty
        payload = {
            "success": True,
            "record_model": model,
            "record": _attach_public_url(instance, record),
            "requested_qty": requested_qty,
            "available_qty": available_qty,
            "can_fulfill": can_fulfill if available_qty is not None else None,
            "stock_source": "product_fields",
        }
        _audit_event(
            "whatsapp_stock_lookup",
            instance=instance,
            payload={
                "product_id": int(product_id),
                "product_model": model,
                "requested_qty": requested_qty,
                "available_qty": available_qty,
                "can_fulfill": payload["can_fulfill"],
            },
        )
        return payload
    except Exception as exc:
        _audit_event(
            "whatsapp_stock_lookup_error",
            instance=_instance_from_request(request),
            payload={"error": str(exc), "product_id": product_id},
            level="error",
        )
        return _tool_error(str(exc))


@mcp.tool
def crear_cotizacion_whatsapp(
    partner_id: int,
    items: list[dict[str, Any]],
    whatsapp_phone: str,
    notes: str | None = None,
    client_order_ref: str | None = None,
    origin: str | None = None,
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
        normalized_items = _normalize_quote_items(items)
        if not normalized_items:
            return _tool_error("At least one item is required")

        values: dict[str, Any] = {"partner_id": int(partner_id)}
        if client_order_ref:
            values["client_order_ref"] = client_order_ref.strip()
        if origin:
            values["origin"] = origin.strip()
        _append_order_origin(values, whatsapp_phone=whatsapp_phone, notes=notes)
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
        created_lines: list[dict[str, Any]] = []
        for item in normalized_items:
            resolved_product_id = _resolve_variant_id(client, item["product_model"], int(item["product_id"]))
            line_values: dict[str, Any] = {
                "order_id": int(order_id),
                "product_id": resolved_product_id,
                "product_uom_qty": float(item["quantity"]),
            }
            if item.get("name"):
                line_values["name"] = item["name"]
            if item.get("uom_id") is not None:
                line_values["product_uom"] = int(item["uom_id"])
            line_id = client.create("sale.order.line", line_values)
            created_lines.append(_read_single_record(client, "sale.order.line", line_id, _sale_order_line_fields()))

        order_payload = _read_order_with_lines(client, order_id)
        order = order_payload["order"]
        partner = _read_partner(client, int(partner_id))
        message_body = "\n".join(
            [
                "Cotización creada desde WhatsApp.",
                f"WhatsApp: {whatsapp_phone}",
                f"Items: {len(normalized_items)}",
            ]
        )
        client.call_method(
            "sale.order",
            "message_post",
            ids=[int(order_id)],
            kwargs={"body": message_body, "subtype_xmlid": "mail.mt_note"},
        )
        _audit_event(
            "whatsapp_quote_created",
            instance=instance,
            phone=whatsapp_phone,
            partner_id=int(partner_id),
            order_id=int(order_id),
            payload={"items": normalized_items, "line_count": len(created_lines)},
        )
        return {
            "success": True,
            "created": True,
            "order_id": int(order_id),
            "order": order,
            "lines": order_payload["lines"],
            "line_count": order_payload["line_count"],
            "partner": partner,
        }
    except Exception as exc:
        _audit_event(
            "whatsapp_quote_create_error",
            instance=_instance_from_request(request),
            phone=whatsapp_phone,
            partner_id=partner_id,
            payload={"error": str(exc)},
            level="error",
        )
        return _tool_error(str(exc))


@mcp.tool
def actualizar_cotizacion_whatsapp(
    order_id: int,
    items: list[dict[str, Any]],
    whatsapp_phone: str | None = None,
    notes: str | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        normalized_items = _normalize_quote_items(items)
        if not normalized_items:
            return _tool_error("At least one item is required")

        order_payload = _read_order_with_lines(client, int(order_id))
        if order_payload["order"].get("state") not in {"draft", "sent"}:
            return _tool_error("Only draft or sent quotations can be updated")

        existing_lines = order_payload["lines"]
        updated_line_ids: list[int] = []
        created_line_ids: list[int] = []
        for item in normalized_items:
            resolved_product_id = _resolve_variant_id(client, item["product_model"], int(item["product_id"]))
            line_match = None
            for line in existing_lines:
                if _m2o_id(line.get("product_id")) == resolved_product_id:
                    line_match = line
                    break

            if line_match is not None:
                values: dict[str, Any] = {"product_uom_qty": float(item["quantity"])}
                if item.get("name"):
                    values["name"] = item["name"]
                if item.get("uom_id") is not None:
                    values["product_uom"] = int(item["uom_id"])
                client.write("sale.order.line", [int(line_match["id"])], values)
                updated_line_ids.append(int(line_match["id"]))
            else:
                line_values = {
                    "order_id": int(order_id),
                    "product_id": resolved_product_id,
                    "product_uom_qty": float(item["quantity"]),
                }
                if item.get("name"):
                    line_values["name"] = item["name"]
                if item.get("uom_id") is not None:
                    line_values["product_uom"] = int(item["uom_id"])
                line_id = client.create("sale.order.line", line_values)
                created_line_ids.append(int(line_id))

        if notes:
            current_note = order_payload["order"].get("note") or ""
            merged_note = "\n".join([part for part in (current_note.strip(), notes.strip()) if part])
            client.write("sale.order", [int(order_id)], {"note": merged_note})

        refreshed = _read_order_with_lines(client, int(order_id))
        _audit_event(
            "whatsapp_quote_updated",
            instance=instance,
            phone=whatsapp_phone,
            order_id=int(order_id),
            payload={
                "updated_line_ids": updated_line_ids,
                "created_line_ids": created_line_ids,
                "items": normalized_items,
            },
        )
        return {
            "success": True,
            "updated": True,
            "order_id": int(order_id),
            "order": refreshed["order"],
            "lines": refreshed["lines"],
            "line_count": refreshed["line_count"],
            "updated_line_ids": updated_line_ids,
            "created_line_ids": created_line_ids,
        }
    except Exception as exc:
        _audit_event(
            "whatsapp_quote_update_error",
            instance=_instance_from_request(request),
            phone=whatsapp_phone,
            order_id=order_id,
            payload={"error": str(exc)},
            level="error",
        )
        return _tool_error(str(exc))


@mcp.tool
def confirmar_cotizacion_whatsapp(
    order_id: int,
    confirmation_text: str,
    whatsapp_phone: str | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        if not _is_clear_confirmation(confirmation_text):
            return _tool_error("Confirmation text is not explicit enough")
        client = _client_from_instance(instance)
        order_payload = _read_order_with_lines(client, int(order_id))
        order = order_payload["order"]
        if order.get("state") not in {"draft", "sent"}:
            return _tool_error("Only pending quotations can be confirmed")
        result = client.call_method("sale.order", "action_confirm", ids=[int(order_id)])
        refreshed = _read_order_with_lines(client, int(order_id))
        client.call_method(
            "sale.order",
            "message_post",
            ids=[int(order_id)],
            kwargs={
                "body": f"Orden confirmada por WhatsApp. Texto de confirmación: {confirmation_text.strip()}",
                "subtype_xmlid": "mail.mt_note",
            },
        )
        _audit_event(
            "whatsapp_quote_confirmed",
            instance=instance,
            phone=whatsapp_phone,
            order_id=int(order_id),
            payload={"confirmation_text": confirmation_text.strip(), "result": result},
        )
        return {
            "success": True,
            "confirmed": True,
            "result": result,
            "order": refreshed["order"],
            "lines": refreshed["lines"],
            "line_count": refreshed["line_count"],
        }
    except Exception as exc:
        _audit_event(
            "whatsapp_quote_confirm_error",
            instance=_instance_from_request(request),
            phone=whatsapp_phone,
            order_id=order_id,
            payload={"error": str(exc)},
            level="error",
        )
        return _tool_error(str(exc))


@mcp.tool
def consultar_estado_pedido(
    phone: str,
    order_name: str | None = None,
    limit: int = 5,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        query_value = (phone or "").strip()
        if not query_value and not (order_name or "").strip():
            return _tool_error("Phone or order name is required")
        limit_value = _validate_limit(limit)
        client = _client_from_instance(instance)
        conditions: list[tuple[str, str, Any]] = []
        if query_value:
            conditions.extend(
                [
                    ("partner_id.phone", "ilike", query_value),
                    ("partner_id.mobile", "ilike", query_value),
                ]
            )
        if order_name and order_name.strip():
            order_query = order_name.strip()
            conditions.extend(
                [
                    ("name", "ilike", order_query),
                    ("client_order_ref", "ilike", order_query),
                ]
            )
        domain = _or_domain(conditions)
        results = client.search_read(
            "sale.order",
            domain,
            ["id", "name", "partner_id", "client_order_ref", "origin", "amount_total", "state", "date_order", "currency_id", "activity_state"],
            limit=limit_value,
        )
        _audit_event(
            "whatsapp_order_lookup",
            instance=instance,
            phone=query_value,
            payload={"count": len(results), "order_name": order_name},
        )
        return {"success": True, "count": len(results), "results": results}
    except Exception as exc:
        _audit_event(
            "whatsapp_order_lookup_error",
            instance=_instance_from_request(request),
            phone=phone,
            payload={"error": str(exc)},
            level="error",
        )
        return _tool_error(str(exc))


@mcp.tool
def crear_actividad_para_vendedor(
    partner_id: int,
    summary: str,
    reason: str,
    whatsapp_phone: str | None = None,
    deadline: str | None = None,
    user_id: int | None = None,
    request: Request = CurrentRequest(),
) -> dict[str, Any]:
    try:
        instance = _instance_from_request(request)
        client = _client_from_instance(instance)
        activity_type_id = _find_activity_type_id(client)
        if activity_type_id is None:
            return _tool_error("No activity type is available in Odoo")

        note = "\n".join(
            part
            for part in (
                f"Motivo: {reason.strip()}",
                f"WhatsApp: {whatsapp_phone}" if whatsapp_phone else "",
            )
            if part
        )
        result = client.call_method(
            "res.partner",
            "activity_schedule",
            ids=[int(partner_id)],
            kwargs={
                "activity_type_id": activity_type_id,
                "summary": summary.strip(),
                **({"date_deadline": deadline.strip()} if deadline and deadline.strip() else {}),
                **({"note": note} if note else {}),
                **({"user_id": int(user_id)} if user_id is not None else {}),
            },
        )
        client.call_method(
            "res.partner",
            "message_post",
            ids=[int(partner_id)],
            kwargs={
                "body": f"Escalamiento comercial registrado. Motivo: {reason.strip()}",
                "subtype_xmlid": "mail.mt_note",
            },
        )
        _audit_event(
            "whatsapp_escalation_created",
            instance=instance,
            phone=whatsapp_phone,
            partner_id=int(partner_id),
            payload={"summary": summary.strip(), "reason": reason.strip(), "activity_type_id": activity_type_id},
        )
        return {"success": True, "result": result, "activity_type_id": activity_type_id}
    except Exception as exc:
        _audit_event(
            "whatsapp_escalation_error",
            instance=_instance_from_request(request),
            phone=whatsapp_phone,
            partner_id=partner_id,
            payload={"error": str(exc)},
            level="error",
        )
        return _tool_error(str(exc))
