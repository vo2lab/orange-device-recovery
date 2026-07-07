"""Token and password helpers for local recovery sessions."""

from __future__ import annotations

import hmac
import secrets
import string


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def generate_hotspot_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(12, length)))


def bearer_token(headers: dict[str, str]) -> str:
    value = headers.get("authorization") or headers.get("Authorization") or ""
    prefix = "Bearer "
    if not value.startswith(prefix):
        return ""
    return value[len(prefix):].strip()


def token_matches(provided: str, expected: str) -> bool:
    return bool(provided and expected and hmac.compare_digest(provided, expected))
