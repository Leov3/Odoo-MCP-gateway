from __future__ import annotations

import sqlite3
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import unicodedata

DB_PATH = Path("/data/app.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    database_name TEXT NOT NULL,
    username TEXT NOT NULL,
    secret_encrypted TEXT NOT NULL,
    version TEXT NOT NULL,
    api_mode TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);
"""

ALLOWED_VERSIONS = {"16", "17", "18", "19"}
ALLOWED_API_MODES = {"xmlrpc", "json2"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_instance_slug(name: str) -> str:
    value = unicodedata.normalize("NFKD", name or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "instance"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "url": row["url"],
        "database_name": row["database_name"],
        "username": row["username"],
        "secret_encrypted": row["secret_encrypted"],
        "version": row["version"],
        "api_mode": row["api_mode"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _admin_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_salt": row["password_salt"],
        "password_hash": row["password_hash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@contextmanager
def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
        _ensure_instance_slug_column(conn)


def _get_instance_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(instances)").fetchall()
    return {row["name"] for row in rows}


def _iter_instances(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM instances ORDER BY id ASC").fetchall()


def _slug_exists(conn: sqlite3.Connection, slug: str, instance_id: int | None = None) -> bool:
    query = "SELECT 1 FROM instances WHERE slug = ?"
    params: list[Any] = [slug]
    if instance_id is not None:
        query += " AND id != ?"
        params.append(instance_id)
    query += " LIMIT 1"
    row = conn.execute(query, tuple(params)).fetchone()
    return row is not None


def _reserve_unique_slug(conn: sqlite3.Connection, base_slug: str, instance_id: int | None = None) -> str:
    slug = base_slug or "instance"
    suffix = 2
    while _slug_exists(conn, slug, instance_id=instance_id):
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


def _require_unique_slug(conn: sqlite3.Connection, slug: str, instance_id: int | None = None) -> None:
    if _slug_exists(conn, slug, instance_id=instance_id):
        raise ValueError("An instance with a similar route already exists")


def _ensure_instance_slug_column(conn: sqlite3.Connection) -> None:
    columns = _get_instance_columns(conn)
    if "slug" not in columns:
        conn.execute("ALTER TABLE instances ADD COLUMN slug TEXT")

    rows = _iter_instances(conn)
    for row in rows:
        current_slug = row["slug"] if "slug" in row.keys() else None
        base_slug = normalize_instance_slug(row["name"])
        slug = current_slug or base_slug
        slug = _reserve_unique_slug(conn, slug, instance_id=row["id"])
        if current_slug != slug:
            conn.execute(
                "UPDATE instances SET slug = ?, updated_at = ? WHERE id = ?",
                (slug, now_iso(), row["id"]),
            )

    if "slug" in _get_instance_columns(conn):
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_slug ON instances(slug)")

def list_instances(active_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM instances"
    params: tuple[Any, ...] = ()
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name COLLATE NOCASE ASC"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def count_instances() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM instances").fetchone()
    return int(row["count"] if row else 0)


def count_active_instances() -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM instances WHERE active = 1"
        ).fetchone()
    return int(row["count"] if row else 0)


def get_instance(instance_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
    return _row_to_dict(row)


def get_instance_by_slug(slug: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM instances WHERE slug = ?", (slug,)).fetchone()
    return _row_to_dict(row)


def get_instance_by_name(name: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM instances WHERE name = ?", (name,)).fetchone()
    return _row_to_dict(row)


def create_instance(values: dict[str, Any]) -> int:
    fields = {
        "name": values["name"].strip(),
        "slug": normalize_instance_slug(values.get("slug") or values["name"]),
        "url": values["url"].strip(),
        "database_name": values["database_name"].strip(),
        "username": values["username"].strip(),
        "secret_encrypted": values["secret_encrypted"],
        "version": values["version"].strip(),
        "api_mode": values["api_mode"].strip(),
        "active": 1 if values.get("active", True) else 0,
        "updated_at": now_iso(),
    }
    try:
        with get_connection() as conn:
            _require_unique_slug(conn, fields["slug"])
            cursor = conn.execute(
                """
                INSERT INTO instances
                (name, slug, url, database_name, username, secret_encrypted, version, api_mode, active, updated_at)
                VALUES
                (:name, :slug, :url, :database_name, :username, :secret_encrypted, :version, :api_mode, :active, :updated_at)
                """,
                fields,
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("An instance with that name already exists") from exc
    return int(cursor.lastrowid)


def update_instance(instance_id: int, values: dict[str, Any]) -> None:
    allowed_fields = {
        "name",
        "slug",
        "url",
        "database_name",
        "username",
        "secret_encrypted",
        "version",
        "api_mode",
        "active",
    }
    updates: list[str] = []
    params: dict[str, Any] = {}

    for field in allowed_fields:
        if field in values:
            updates.append(f"{field} = :{field}")
            params[field] = normalize_instance_slug(values[field]) if field == "slug" else values[field]

    if not updates:
        return

    params["updated_at"] = now_iso()
    params["id"] = instance_id
    updates.append("updated_at = :updated_at")

    sql = f"UPDATE instances SET {', '.join(updates)} WHERE id = :id"
    try:
        with get_connection() as conn:
            if "slug" in params:
                _require_unique_slug(conn, params["slug"], instance_id=instance_id)
            conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise ValueError("An instance with that name already exists") from exc


def toggle_instance(instance_id: int) -> None:
    instance = get_instance(instance_id)
    if not instance:
        return
    new_value = 0 if instance["active"] else 1
    update_instance(instance_id, {"active": new_value})


def delete_instance(instance_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM instances WHERE id = ?", (instance_id,))


def get_admin_account() -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM admin_account WHERE id = 1").fetchone()
    return _admin_row_to_dict(row)


def upsert_admin_account(username: str, password_salt: str, password_hash: str) -> None:
    payload = {
        "id": 1,
        "username": username.strip(),
        "password_salt": password_salt,
        "password_hash": password_hash,
        "updated_at": now_iso(),
    }
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO admin_account (id, username, password_salt, password_hash, updated_at)
            VALUES (:id, :username, :password_salt, :password_hash, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                password_salt = excluded.password_salt,
                password_hash = excluded.password_hash,
                updated_at = excluded.updated_at
            """,
            payload,
        )
