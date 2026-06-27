from __future__ import annotations

import os
import hashlib
import secrets
from functools import lru_cache
from hmac import compare_digest

from cryptography.fernet import Fernet

from app.db import get_admin_account


@lru_cache(maxsize=1)
def get_admin_credentials() -> tuple[str, str]:
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "change_me")
    return admin_user, admin_password


@lru_cache(maxsize=1)
def get_secret_key() -> str:
    key = os.getenv("APP_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("APP_SECRET_KEY is required")
    return key


@lru_cache(maxsize=1)
def get_encryption_key() -> bytes:
    key = os.getenv("ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is required")
    try:
        Fernet(key.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError("ENCRYPTION_KEY is invalid") from exc
    return key.encode("utf-8")


def get_fernet() -> Fernet:
    return Fernet(get_encryption_key())


def encrypt_secret(secret: str) -> str:
    if not secret:
        raise ValueError("Secret cannot be empty")
    return get_fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_secret(secret_encrypted: str) -> str:
    if not secret_encrypted:
        return ""
    return get_fernet().decrypt(secret_encrypted.encode("utf-8")).decode("utf-8")


def hash_admin_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if not password:
        raise ValueError("Password cannot be empty")
    salt_value = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_value),
        200_000,
    ).hex()
    return salt_value, password_hash


def verify_admin_password(password: str, password_salt: str, password_hash: str) -> bool:
    _, candidate_hash = hash_admin_password(password, salt=password_salt)
    return compare_digest(candidate_hash, password_hash)


def verify_admin_credentials(username: str, password: str) -> bool:
    account = get_admin_account()
    if not account:
        return False
    return compare_digest(username or "", account["username"]) and verify_admin_password(
        password,
        account["password_salt"],
        account["password_hash"],
    )
