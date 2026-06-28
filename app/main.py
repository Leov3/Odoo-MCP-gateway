from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers
from starlette.middleware.sessions import SessionMiddleware

from app.db import (
    ALLOWED_API_MODES,
    ALLOWED_VERSIONS,
    get_admin_account,
    count_active_instances,
    count_instances,
    create_instance,
    delete_instance,
    get_instance,
    get_instance_by_slug,
    init_db,
    list_instances,
    normalize_instance_slug,
    upsert_admin_account,
    toggle_instance,
    update_instance,
)
from app.mcp_server import build_instance_mcp_app
from app.security import (
    decrypt_secret,
    encrypt_secret,
    get_admin_credentials,
    get_secret_key,
    hash_admin_password,
    verify_admin_credentials,
)

load_dotenv()

templates = Jinja2Templates(directory="app/templates")

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    get_secret_key()
    if not MCP_BEARER_TOKEN:
        raise RuntimeError("MCP_BEARER_TOKEN is required")
    init_db()
    ensure_admin_account()
    yield


app = FastAPI(title="Odoo MCP Gateway", lifespan=app_lifespan)
app.add_middleware(SessionMiddleware, secret_key=get_secret_key(), same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

MCP_BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN", "").strip()


class InstanceMCPMiddleware:
    def __init__(self, wrapped_app: FastAPI):
        self.wrapped_app = wrapped_app
        self._app_cache: dict[int, tuple[str | None, Any]] = {}

    def _parse_instance_path(self, path: str) -> tuple[str, str] | None:
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) < 2 or segments[1] != "mcp":
            return None
        slug = segments[0]
        suffix = "/" + "/".join(segments[2:]) if len(segments) > 2 else "/"
        return slug, suffix

    def _normalize_token(self, value: str) -> str:
        token = (value or "").strip()
        for _ in range(2):
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
        return token

    def _candidate_tokens(self, headers: Headers) -> list[str]:
        candidates: list[str] = []
        auth_header = headers.get("authorization", "")
        if auth_header:
            scheme, _, token = auth_header.partition(" ")
            if scheme.lower() == "bearer" and token:
                candidates.append(self._normalize_token(token))
            else:
                candidates.append(self._normalize_token(auth_header))
        for header_name in ("proxy-authorization", "x-mcp-token", "x-api-key", "x-auth-token"):
            header_value = headers.get(header_name, "")
            if header_value:
                candidates.append(self._normalize_token(header_value))
        return [candidate for candidate in candidates if candidate]

    def _is_authorized(self, headers: Headers) -> bool:
        return any(candidate == MCP_BEARER_TOKEN for candidate in self._candidate_tokens(headers))

    def _get_instance_app(self, instance: dict[str, Any]):
        cache_key = int(instance["id"])
        cache_version = instance.get("updated_at") or instance.get("created_at")
        cached = self._app_cache.get(cache_key)
        if cached and cached[0] == cache_version:
            return cached[1]
        instance_app = build_instance_mcp_app(instance)
        self._app_cache[cache_key] = (cache_version, instance_app)
        return instance_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.wrapped_app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            response = JSONResponse(
                {
                    "detail": "Deprecated endpoint. Use /<instance>/mcp/ for the configured instance.",
                },
                status_code=410,
            )
            await response(scope, receive, send)
            return

        parsed = self._parse_instance_path(path)
        if parsed:
            slug, suffix = parsed
            headers = Headers(scope=scope)
            if not self._is_authorized(headers):
                response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return

            instance = get_instance_by_slug(slug)
            if not instance:
                response = JSONResponse(
                    {"detail": f'Instance "{slug}" was not found'},
                    status_code=404,
                )
                await response(scope, receive, send)
                return

            if not instance["active"]:
                response = JSONResponse(
                    {"detail": f'Instance "{instance["name"]}" is inactive'},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

            instance_app = self._get_instance_app(instance)
            sub_scope = dict(scope)
            sub_scope["path"] = suffix
            sub_scope["raw_path"] = suffix.encode("utf-8")
            root_path = scope.get("root_path", "")
            sub_scope["root_path"] = f"{root_path.rstrip('/')}/{slug}/mcp".rstrip("/")
            sub_scope["state"] = dict(scope.get("state") or {})
            sub_scope["state"]["odoo_instance"] = instance
            await instance_app(sub_scope, receive, send)
            return

        await self.wrapped_app(scope, receive, send)


app.add_middleware(InstanceMCPMiddleware)


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("flashes", []).append({"message": message, "category": category})


def pop_flashes(request: Request) -> list[dict[str, str]]:
    flashes = request.session.pop("flashes", [])
    return flashes if isinstance(flashes, list) else []


def suggest_api_mode(version: str | None) -> str:
    return "json2" if str(version) == "19" else "xmlrpc"


def ensure_admin_account() -> None:
    if get_admin_account():
        return
    default_username, default_password = get_admin_credentials()
    password_salt, password_hash = hash_admin_password(default_password)
    upsert_admin_account(default_username, password_salt, password_hash)


def render_template(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
) -> HTMLResponse:
    payload = {
        "request": request,
        "flashes": pop_flashes(request),
        "is_authenticated": request.session.get("admin_logged_in", False),
        "admin_username": request.session.get("admin_username"),
        "suggest_api_mode": suggest_api_mode,
    }
    if context:
        payload.update(context)
    return templates.TemplateResponse(request, template_name, payload)


def require_admin(request: Request):
    if not request.session.get("admin_logged_in"):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def build_public_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def build_instance_mcp_url(request: Request, slug: str) -> str:
    return f"{build_public_base_url(request)}/{slug}/mcp/"


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_get(request: Request):
    if request.session.get("admin_logged_in"):
        return RedirectResponse(url="/admin", status_code=303)
    return render_template(request, "login.html")


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if verify_admin_credentials(username.strip(), password):
        request.session["admin_logged_in"] = True
        request.session["admin_username"] = username.strip()
        flash(request, "Login successful", "success")
        return RedirectResponse(url="/admin", status_code=303)
    flash(request, "Invalid username or password", "error")
    return render_template(request, "login.html", {"username": username})


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    redirect = require_admin(request)
    if redirect:
        return redirect
    return render_template(
        request,
        "dashboard.html",
        {
            "total_instances": count_instances(),
            "active_instances": count_active_instances(),
            "mcp_endpoint": "/{slug}/mcp/",
            "mcp_warning": "Each instance now has its own MCP endpoint. The legacy /mcp route is deprecated.",
            "admin_username": request.session.get("admin_username"),
        },
    )


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_get(request: Request):
    redirect = require_admin(request)
    if redirect:
        return redirect
    account = get_admin_account()
    return render_template(
        request,
        "settings.html",
        {
            "account": account,
            "form_username": account["username"] if account else "",
        },
    )


@app.post("/admin/settings", response_class=HTMLResponse)
def admin_settings_post(
    request: Request,
    username: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(""),
    confirm_new_password: str = Form(""),
):
    redirect = require_admin(request)
    if redirect:
        return redirect

    account = get_admin_account()
    if not account:
        flash(request, "Admin account not initialized", "error")
        return RedirectResponse(url="/admin/settings", status_code=303)

    if not verify_admin_credentials(account["username"], current_password):
        return render_template(
            request,
            "settings.html",
            {
                "account": account,
                "form_username": username,
                "errors": ["Current password is incorrect"],
            },
        )

    username = username.strip()
    if not username:
        return render_template(
            request,
            "settings.html",
            {
                "account": account,
                "form_username": username,
                "errors": ["Username is required"],
            },
        )

    if new_password or confirm_new_password:
        if new_password != confirm_new_password:
            return render_template(
                request,
                "settings.html",
                {
                    "account": account,
                    "form_username": username,
                    "errors": ["New password and confirmation do not match"],
                },
            )
        password_salt, password_hash = hash_admin_password(new_password)
    else:
        password_salt = account["password_salt"]
        password_hash = account["password_hash"]

    try:
        upsert_admin_account(username, password_salt, password_hash)
        request.session["admin_username"] = username
        flash(request, "Account updated successfully", "success")
        return RedirectResponse(url="/admin/settings", status_code=303)
    except Exception as exc:
        return render_template(
            request,
            "settings.html",
            {
                "account": account,
                "form_username": username,
                "errors": [str(exc)],
            },
        )


@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    request.session["flashes"] = [{"message": "Logged out", "category": "info"}]
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/admin/instances", response_class=HTMLResponse)
def instances_list(request: Request):
    redirect = require_admin(request)
    if redirect:
        return redirect
    return render_template(
        request,
        "instances.html",
        {
            "instances": list_instances(active_only=False),
        },
    )


@app.get("/admin/instances/{instance_id}", response_class=HTMLResponse)
def instance_detail_get(request: Request, instance_id: int):
    redirect = require_admin(request)
    if redirect:
        return redirect

    instance = get_instance(instance_id)
    if not instance:
        flash(request, "Instance not found", "error")
        return RedirectResponse(url="/admin/instances", status_code=303)

    slug = instance["slug"] or normalize_instance_slug(instance["name"])
    mcp_url = build_instance_mcp_url(request, slug)
    return render_template(
        request,
        "instance_detail.html",
        {
            "instance": instance,
            "instance_slug": slug,
            "secret_plaintext": decrypt_secret(instance["secret_encrypted"]),
            "mcp_url": mcp_url,
            "legacy_mcp_url": f"{build_public_base_url(request)}/mcp/",
            "curl_example": f'curl -H "Authorization: Bearer <MCP_BEARER_TOKEN>" "{mcp_url}"',
            "n8n_endpoint": mcp_url,
            "n8n_auth_header": "Authorization: Bearer <MCP_BEARER_TOKEN>",
        },
    )


def _validate_instance_form(
    name: str,
    url: str,
    database_name: str,
    username: str,
    secret: str,
    version: str,
    api_mode: str,
    is_edit: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    values = {
        "name": name.strip(),
        "url": url.strip(),
        "database_name": database_name.strip(),
        "username": username.strip(),
        "version": version.strip(),
        "api_mode": api_mode.strip(),
    }

    for field, value in values.items():
        if not value:
            errors.append(f"{field.replace('_', ' ').title()} is required")

    if values["version"] and values["version"] not in ALLOWED_VERSIONS:
        errors.append("Version must be 16, 17, 18, or 19")

    if values["api_mode"] and values["api_mode"] not in ALLOWED_API_MODES:
        errors.append("API mode must be xmlrpc or json2")

    if not is_edit and not secret.strip():
        errors.append("Secret is required")

    if errors:
        return None, errors

    if secret.strip():
        values["secret_encrypted"] = encrypt_secret(secret.strip())
    return values, []


@app.get("/admin/instances/new", response_class=HTMLResponse)
def instance_new_get(request: Request):
    redirect = require_admin(request)
    if redirect:
        return redirect
    default_version = "19"
    return render_template(
        request,
        "instance_form.html",
        {
            "form_title": "Create instance",
            "form_action": "/admin/instances/new",
            "instance": {
                "name": "",
                "url": "",
                "database_name": "",
                "username": "",
                "version": default_version,
                "api_mode": suggest_api_mode(default_version),
                "secret": "",
                "active": True,
            },
            "versions": sorted(ALLOWED_VERSIONS),
            "api_modes": sorted(ALLOWED_API_MODES),
            "is_edit": False,
        },
    )


@app.post("/admin/instances/new", response_class=HTMLResponse)
def instance_new_post(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    database_name: str = Form(...),
    username: str = Form(...),
    secret: str = Form(""),
    version: str = Form(...),
    api_mode: str = Form(...),
    active: str | None = Form(None),
):
    redirect = require_admin(request)
    if redirect:
        return redirect
    values, errors = _validate_instance_form(name, url, database_name, username, secret, version, api_mode)
    if errors:
        return render_template(
            request,
            "instance_form.html",
            {
                "form_title": "Create instance",
                "form_action": "/admin/instances/new",
                "instance": {
                    "name": name,
                    "url": url,
                    "database_name": database_name,
                    "username": username,
                    "version": version,
                    "api_mode": api_mode,
                    "secret": "",
                    "active": active is not None,
                },
                "versions": sorted(ALLOWED_VERSIONS),
                "api_modes": sorted(ALLOWED_API_MODES),
                "errors": errors,
                "is_edit": False,
            },
        )
    try:
        values["active"] = active is not None
        create_instance(values)
        flash(request, "Instance created successfully", "success")
        return RedirectResponse(url="/admin/instances", status_code=303)
    except Exception as exc:
        return render_template(
            request,
            "instance_form.html",
            {
                "form_title": "Create instance",
                "form_action": "/admin/instances/new",
                "instance": {
                    "name": name,
                    "url": url,
                    "database_name": database_name,
                    "username": username,
                    "version": version,
                    "api_mode": api_mode,
                    "secret": "",
                    "active": active is not None,
                },
                "versions": sorted(ALLOWED_VERSIONS),
                "api_modes": sorted(ALLOWED_API_MODES),
                "errors": [str(exc)],
                "is_edit": False,
            },
        )


@app.get("/admin/instances/{instance_id}/edit", response_class=HTMLResponse)
def instance_edit_get(request: Request, instance_id: int):
    redirect = require_admin(request)
    if redirect:
        return redirect
    instance = get_instance(instance_id)
    if not instance:
        flash(request, "Instance not found", "error")
        return RedirectResponse(url="/admin/instances", status_code=303)
    return render_template(
        request,
        "instance_form.html",
        {
            "form_title": "Edit instance",
            "form_action": f"/admin/instances/{instance_id}/edit",
            "instance": {
                **instance,
                "secret": "",
            },
            "versions": sorted(ALLOWED_VERSIONS),
            "api_modes": sorted(ALLOWED_API_MODES),
            "is_edit": True,
        },
    )


@app.post("/admin/instances/{instance_id}/edit", response_class=HTMLResponse)
def instance_edit_post(
    request: Request,
    instance_id: int,
    name: str = Form(...),
    url: str = Form(...),
    database_name: str = Form(...),
    username: str = Form(...),
    secret: str = Form(""),
    version: str = Form(...),
    api_mode: str = Form(...),
    active: str | None = Form(None),
):
    redirect = require_admin(request)
    if redirect:
        return redirect
    instance = get_instance(instance_id)
    if not instance:
        flash(request, "Instance not found", "error")
        return RedirectResponse(url="/admin/instances", status_code=303)
    values, errors = _validate_instance_form(
        name, url, database_name, username, secret, version, api_mode, is_edit=True
    )
    if errors:
        return render_template(
            request,
            "instance_form.html",
            {
                "form_title": "Edit instance",
                "form_action": f"/admin/instances/{instance_id}/edit",
                "instance": {
                    "id": instance_id,
                    "name": name,
                    "url": url,
                    "database_name": database_name,
                    "username": username,
                    "version": version,
                    "api_mode": api_mode,
                    "secret": "",
                    "active": active is not None,
                },
                "versions": sorted(ALLOWED_VERSIONS),
                "api_modes": sorted(ALLOWED_API_MODES),
                "errors": errors,
                "is_edit": True,
            },
    )
    try:
        values["active"] = active is not None
        values["slug"] = normalize_instance_slug(values["name"])
        if not secret.strip():
            values.pop("secret_encrypted", None)
        update_instance(instance_id, values)
        flash(request, "Instance updated successfully", "success")
        return RedirectResponse(url="/admin/instances", status_code=303)
    except Exception as exc:
        return render_template(
            request,
            "instance_form.html",
            {
                "form_title": "Edit instance",
                "form_action": f"/admin/instances/{instance_id}/edit",
                "instance": {
                    "id": instance_id,
                    "name": name,
                    "url": url,
                    "database_name": database_name,
                    "username": username,
                    "version": version,
                    "api_mode": api_mode,
                    "secret": "",
                    "active": active is not None,
                },
                "versions": sorted(ALLOWED_VERSIONS),
                "api_modes": sorted(ALLOWED_API_MODES),
                "errors": [str(exc)],
                "is_edit": True,
            },
        )


def _get_instance_or_flash(request: Request, instance_id: int):
    instance = get_instance(instance_id)
    if not instance:
        flash(request, "Instance not found", "error")
        return None
    return instance


@app.post("/admin/instances/{instance_id}/test")
def instance_test(request: Request, instance_id: int):
    redirect = require_admin(request)
    if redirect:
        return redirect
    instance = _get_instance_or_flash(request, instance_id)
    if not instance:
        return RedirectResponse(url="/admin/instances", status_code=303)
    from app.odoo_client import OdooClient, OdooInstanceConfig

    try:
        client = OdooClient(
            OdooInstanceConfig(
                url=instance["url"],
                database_name=instance["database_name"],
                username=instance["username"],
                secret_encrypted=instance["secret_encrypted"],
                version=instance["version"],
                api_mode=instance["api_mode"],
            )
        )
        if instance["api_mode"] == "xmlrpc":
            client.authenticate()
        else:
            client.search_read("res.partner", [], ["id"], limit=1)
        flash(request, f'Connection test successful for "{instance["name"]}"', "success")
    except Exception as exc:
        flash(request, f'Connection test failed for "{instance["name"]}": {exc}', "error")
    return RedirectResponse(url="/admin/instances", status_code=303)


@app.post("/admin/instances/{instance_id}/toggle")
def instance_toggle(request: Request, instance_id: int):
    redirect = require_admin(request)
    if redirect:
        return redirect
    instance = _get_instance_or_flash(request, instance_id)
    if not instance:
        return RedirectResponse(url="/admin/instances", status_code=303)
    toggle_instance(instance_id)
    flash(
        request,
        f'Instance "{instance["name"]}" {"activated" if not instance["active"] else "deactivated"}',
        "success",
    )
    return RedirectResponse(url="/admin/instances", status_code=303)


@app.post("/admin/instances/{instance_id}/delete")
def instance_delete(request: Request, instance_id: int):
    redirect = require_admin(request)
    if redirect:
        return redirect
    instance = _get_instance_or_flash(request, instance_id)
    if not instance:
        return RedirectResponse(url="/admin/instances", status_code=303)
    delete_instance(instance_id)
    flash(request, f'Instance "{instance["name"]}" deleted', "success")
    return RedirectResponse(url="/admin/instances", status_code=303)
