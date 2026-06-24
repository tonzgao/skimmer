"""Password protection for the skimmer UI.

Miniflux-style single-password login: POST the password to /login, get a
signed session cookie, everything else redirects to /login until the cookie
is valid. The cookie value is `expiry|hmac(secret, expiry)` so no server-side
session state is needed. Pure standard library.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

COOKIE_NAME = "skimmer_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days, like Miniflux's "remember me"


def make_secret() -> str:
    """Generate the per-installation signing secret."""
    return secrets.token_hex(32)


def check_password(candidate: str | None, expected: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def issue_cookie_value(secret: str, now: float | None = None) -> str:
    expiry = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    return f"{expiry}|{_sign(secret, expiry)}"


def verify_cookie_value(secret: str, value: str | None, now: float | None = None) -> bool:
    if not value or "|" not in value:
        return False
    raw_expiry, signature = value.split("|", 1)
    if not raw_expiry.isdigit():
        return False
    expiry = int(raw_expiry)
    clock = now if now is not None else time.time()
    if clock >= expiry:
        return False
    return hmac.compare_digest(_sign(secret, expiry), signature)


def _sign(secret: str, expiry: int) -> str:
    message = f"skimmer-session:{expiry}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def parse_cookies(header: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not header:
        return cookies
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            cookies[name] = value
    return cookies
