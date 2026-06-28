from __future__ import annotations

import json
import xmlrpc.client
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from app.security import decrypt_secret


class OdooClientError(RuntimeError):
    pass


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def _json_error_message(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"{fallback} (HTTP {response.status_code}): {response.text.strip() or 'Empty response'}"

    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("detail") or json.dumps(payload)
    else:
        message = json.dumps(payload)
    return f"{fallback} (HTTP {response.status_code}): {message}"


def _unwrap_json2_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("result", "results", "data"):
            if key in payload:
                return payload[key]
    return payload


@dataclass(slots=True)
class OdooInstanceConfig:
    url: str
    database_name: str
    username: str
    secret_encrypted: str
    version: str
    api_mode: str

    @property
    def secret(self) -> str:
        return decrypt_secret(self.secret_encrypted)


class OdooClient:
    def __init__(self, instance: OdooInstanceConfig):
        self.instance = instance
        self.base_url = _normalize_base_url(instance.url)
        self.database_name = instance.database_name
        self.username = instance.username
        self.secret = instance.secret
        self.version = instance.version
        self.api_mode = instance.api_mode
        self._uid: int | None = None

    @property
    def uid(self) -> int | None:
        return self._uid

    def _endpoint(self, suffix: str) -> str:
        return urljoin(f"{self.base_url}/", suffix.lstrip("/"))

    def _xmlrpc_common(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(
            self._endpoint("xmlrpc/2/common"),
            allow_none=True,
        )

    def _xmlrpc_object(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(
            self._endpoint("xmlrpc/2/object"),
            allow_none=True,
        )

    def _json_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"bearer {self.secret}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Odoo MCP Gateway",
        }
        if self.database_name:
            headers["X-Odoo-Database"] = self.database_name
        return headers

    def _json_request(self, model: str, method: str, payload: dict[str, Any]) -> Any:
        response = requests.post(
            self._endpoint(f"json/2/{model}/{method}"),
            headers=self._json_headers(),
            json=payload,
            timeout=30,
        )
        if not response.ok:
            raise OdooClientError(
                _json_error_message(response, f"JSON-2 {method} failed for {model}")
            )
        return response.json()

    def authenticate(self) -> int:
        if self.api_mode == "json2":
            response = requests.post(
                self._endpoint("json/2/res.partner/search_read"),
                headers=self._json_headers(),
                json={"domain": [], "fields": ["id"], "limit": 1},
                timeout=30,
            )
            if not response.ok:
                raise OdooClientError(
                    _json_error_message(response, "JSON-2 authentication failed")
                )
            self._uid = 1
            return 1

        common = self._xmlrpc_common()
        try:
            uid = common.authenticate(self.database_name, self.username, self.secret, {})
        except Exception as exc:  # pragma: no cover - network/remote error
            raise OdooClientError(f"XML-RPC authentication failed: {exc}") from exc

        if not uid:
            raise OdooClientError("XML-RPC authentication failed: invalid credentials")
        self._uid = int(uid)
        return self._uid

    def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str],
        load: Any | None = None,
    ) -> list[dict[str, Any]]:
        if not ids:
            return []

        if self.api_mode == "json2":
            payload: dict[str, Any] = {
                "ids": ids,
                "fields": fields,
                "context": {"lang": "en_US"},
            }
            if load is not None:
                payload["load"] = load
            result = self._json_request(model, "read", payload)
            if not isinstance(result, list):
                raise OdooClientError(
                    f"JSON-2 read for {model} returned an unexpected payload"
                )
            return result

        uid = self._uid or self.authenticate()
        object_proxy = self._xmlrpc_object()
        try:
            result = object_proxy.execute_kw(
                self.database_name,
                uid,
                self.secret,
                model,
                "read",
                [ids],
                {"fields": fields, **({"load": load} if load is not None else {})},
            )
        except Exception as exc:  # pragma: no cover - network/remote error
            raise OdooClientError(f"XML-RPC read failed for {model}: {exc}") from exc
        if not isinstance(result, list):
            raise OdooClientError(f"XML-RPC read for {model} returned an unexpected payload")
        return result

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        if self.api_mode == "json2":
            response = requests.post(
                self._endpoint(f"json/2/{model}/search_read"),
                headers=self._json_headers(),
                json={
                    "domain": domain,
                    "fields": fields,
                    "limit": limit,
                    "context": {"lang": "en_US"},
                },
                timeout=30,
            )
            if not response.ok:
                raise OdooClientError(
                    _json_error_message(response, f"JSON-2 search_read failed for {model}")
                )
            payload = _unwrap_json2_payload(response.json())
            if not isinstance(payload, list):
                raise OdooClientError(
                    f"JSON-2 search_read for {model} returned an unexpected payload"
                )
            return payload

        uid = self._uid or self.authenticate()
        object_proxy = self._xmlrpc_object()
        try:
            result = object_proxy.execute_kw(
                self.database_name,
                uid,
                self.secret,
                model,
                "search_read",
                [domain],
                {"fields": fields, "limit": limit},
            )
        except Exception as exc:  # pragma: no cover - network/remote error
            raise OdooClientError(f"XML-RPC search_read failed for {model}: {exc}") from exc
        if not isinstance(result, list):
            raise OdooClientError(f"XML-RPC search_read for {model} returned an unexpected payload")
        return result

    def create(self, model: str, values: dict[str, Any]) -> int:
        if self.api_mode == "json2":
            payload = self._json_request(model, "create", {"vals_list": [values]})
            payload = _unwrap_json2_payload(payload)
            if isinstance(payload, int):
                return payload
            if isinstance(payload, dict) and "id" in payload:
                return int(payload["id"])
            if isinstance(payload, list) and payload:
                first = payload[0]
                if isinstance(first, int):
                    return first
                if isinstance(first, dict) and "id" in first:
                    return int(first["id"])
            raise OdooClientError(f"JSON-2 create for {model} returned an unexpected payload")

        uid = self._uid or self.authenticate()
        object_proxy = self._xmlrpc_object()
        try:
            result = object_proxy.execute_kw(
                self.database_name,
                uid,
                self.secret,
                model,
                "create",
                [values],
            )
        except Exception as exc:  # pragma: no cover - network/remote error
            raise OdooClientError(f"XML-RPC create failed for {model}: {exc}") from exc
        return int(result)

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        if not ids:
            return False

        if self.api_mode == "json2":
            payload = {
                "ids": ids,
                "vals": values,
                "context": {"lang": "en_US"},
            }
            result = self._json_request(model, "write", payload)
            if isinstance(result, bool):
                return result
            if isinstance(result, dict) and "result" in result:
                return bool(result["result"])
            return bool(result)

        uid = self._uid or self.authenticate()
        object_proxy = self._xmlrpc_object()
        try:
            result = object_proxy.execute_kw(
                self.database_name,
                uid,
                self.secret,
                model,
                "write",
                [ids, values],
            )
        except Exception as exc:  # pragma: no cover - network/remote error
            raise OdooClientError(f"XML-RPC write failed for {model}: {exc}") from exc
        return bool(result)

    def call_method(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        kwargs = dict(kwargs or {})

        if self.api_mode == "json2":
            if args:
                raise OdooClientError(
                    "JSON-2 call_method does not support positional arguments"
                )
            payload: dict[str, Any] = {"context": {"lang": "en_US"}, **kwargs}
            if ids is not None:
                payload["ids"] = ids
            return self._json_request(model, method, payload)

        uid = self._uid or self.authenticate()
        object_proxy = self._xmlrpc_object()
        rpc_args: list[Any] = []
        if ids is not None:
            rpc_args.append(ids)
        if args:
            rpc_args.extend(args)
        try:
            result = object_proxy.execute_kw(
                self.database_name,
                uid,
                self.secret,
                model,
                method,
                rpc_args,
                kwargs,
            )
        except Exception as exc:  # pragma: no cover - network/remote error
            raise OdooClientError(
                f"XML-RPC call_method failed for {model}.{method}: {exc}"
            ) from exc
        return result
