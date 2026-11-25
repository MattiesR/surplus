"""Local API protection (plan §9.3).

The API binds to 127.0.0.1, but a malicious web page or DNS rebinding can still reach localhost, so:
- the Host header must be an allowed host (blocks DNS rebinding),
- every /api request needs the session cookie (HttpOnly) or a bearer token (CLI),
- every state-changing request needs a CSRF header equal to the CSRF cookie (double submit).
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import JSONResponse

SESSION_COOKIE = "surplus_session"
CSRF_COOKIE = "surplus_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_PATHS = frozenset({"/api/health", "/auth"})


def load_or_create_token(path: Path) -> str:
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


def csrf_for(token: str) -> str:
    """Derived, so it survives restarts without being stored, and reveals nothing about the token."""
    return hmac.new(token.encode(), b"surplus-csrf", "sha256").hexdigest()


def _eq(a: str | None, b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode(), b.encode())


def make_middleware(
    token: str, allowed_hosts: frozenset[str]
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    csrf = csrf_for(token)

    async def middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = (request.headers.get("host") or "").lower()
        if host not in allowed_hosts:
            return JSONResponse({"detail": "host not allowed"}, status_code=421)
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_PATHS:
            return await call_next(request)
        bearer = request.headers.get("authorization", "")
        authed = _eq(request.cookies.get(SESSION_COOKIE), token) or (
            bearer.startswith("Bearer ") and _eq(bearer[7:], token)
        )
        if not authed:
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        # Bearer clients (the CLI) are not browsers, so CSRF does not apply to them.
        if (
            request.method not in SAFE_METHODS
            and not bearer.startswith("Bearer ")
            and not _eq(request.headers.get(CSRF_HEADER), csrf)
        ):
            return JSONResponse({"detail": "csrf check failed"}, status_code=403)
        return await call_next(request)

    return middleware


def set_auth_cookies(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
    response.set_cookie(
        CSRF_COOKIE, csrf_for(token), httponly=False, samesite="strict", max_age=60 * 60 * 24 * 30
    )
