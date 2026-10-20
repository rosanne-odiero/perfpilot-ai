"""ASGI middleware enforcing request-body size limits.

Bodies are rejected with ``413``: up front from ``Content-Length``, and for chunked or lying
clients by counting the bytes actually received (the app then sees a client disconnect).
Repository ZIP uploads get ``max_upload_bytes`` plus multipart overhead; every other route gets
``max_request_bytes``.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.core.config import Settings
from backend.core.logging import get_logger

log = get_logger(__name__)

UPLOAD_PATHS = frozenset({"/api/repositories/upload"})
MULTIPART_OVERHEAD = 64 * 1024


def _limit_for(path: str, settings: Settings) -> int:
    if path in UPLOAD_PATHS:
        return settings.max_upload_bytes + MULTIPART_OVERHEAD
    return settings.max_request_bytes


async def _reject(send: Send, limit: int, *, status: int = 413) -> None:
    if status == 413:
        code, message = "limit_exceeded", f"request body exceeds the {limit}-byte limit"
    else:
        code, message = "invalid_content_length", "Content-Length header is not a valid size"
    log.warning("request rejected", extra={"status": status, "error_code": code})
    body = json.dumps(
        {"error": {"code": code, "message": message, "details": {"limit_bytes": limit}}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _limit_for(scope.get("path", ""), self.settings)
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                if not value.isdigit():
                    await _reject(send, limit, status=400)
                    return
                if int(value) > limit:
                    await _reject(send, limit)
                    return
                break

        received = 0
        overflow = False
        response_started = False

        async def limited_receive() -> Message:
            # Past the limit the app sees a client disconnect, so no handler (or framework body
            # parser) can turn the overflow into a different error; the 413 is sent below.
            nonlocal received, overflow
            if overflow:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    overflow = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if overflow and not response_started:
                response_started = True
                await _reject(send, limit)
                return
            if overflow:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not overflow:
                raise
        if overflow and not response_started:
            await _reject(send, limit)


_BASE_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)
# Report downloads are attachments, but if a browser ever renders an HTML report inline it must
# not run scripts or load anything: reports are self-contained (inline CSS, inline SVG).
_DOWNLOAD_CSP = (
    b"content-security-policy",
    b"sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        extra = [_DOWNLOAD_CSP] if path.startswith("/api/reports/") else []

        async def add_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers += [(n, v) for n, v in (*_BASE_HEADERS, *extra) if n not in present]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, add_headers)
