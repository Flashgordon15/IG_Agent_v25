"""HTTP middleware — 401 on protected admin/metrics routes without valid session."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api.auth import is_authenticated, path_is_public, path_requires_auth

_SUPERVISION_UA = "IG-Agent-Watchdog/"


def _supervision_health_bypass(request: Request) -> bool:
    """Allow local watchdog curl to read /api/health without dashboard login."""
    if request.url.path != "/api/health":
        return False
    ua = request.headers.get("user-agent", "")
    return ua.startswith(_SUPERVISION_UA)


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        method = request.method.upper()

        if method == "OPTIONS":
            return await call_next(request)

        if path_is_public(path, method) or not path_requires_auth(path):
            return await call_next(request)

        if _supervision_health_bypass(request):
            return await call_next(request)

        if is_authenticated(request):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
        )
