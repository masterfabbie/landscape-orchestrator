"""Browser sessions: a signed cookie that proves the API token was entered."""

from __future__ import annotations

import hashlib
import hmac
import time

SESSION_COOKIE = "lo_session"
SESSION_LIFETIME_SECONDS = 8 * 3600


def _signature(api_token: str, expires: int) -> str:
    message = f"session:{expires}".encode()
    return hmac.new(api_token.encode(), message, hashlib.sha256).hexdigest()


def create_session(api_token: str, now: float | None = None) -> str:
    """Cookie value valid for the session lifetime. Changing the token invalidates it."""
    expires = int((time.time() if now is None else now) + SESSION_LIFETIME_SECONDS)
    return f"{expires}.{_signature(api_token, expires)}"


def is_valid_session(api_token: str, value: str | None, now: float | None = None) -> bool:
    if not value:
        return False
    expires_text, _, signature = value.partition(".")
    if not expires_text.isdigit():
        return False
    expires = int(expires_text)
    if expires < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(signature, _signature(api_token, expires))
