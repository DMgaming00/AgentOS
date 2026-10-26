"""Request logging with correlation ids, and request body size limits."""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentos_core.log import get_logger

log = get_logger("agentos.http")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        request_id = headers.get(b"x-request-id", b"").decode()[:64] or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status: dict[str, Any] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if not path.startswith(("/health", "/ready")):
                log.info(
                    "http_request",
                    method=scope.get("method"),
                    path=path,
                    status=status.get("code"),
                    duration_ms=round((time.perf_counter() - start) * 1000, 1),
                )
            structlog.contextvars.unbind_contextvars("request_id")


class BodySizeLimitMiddleware:
    """Reject bodies over the limit, whether or not Content-Length is declared."""

    def __init__(self, app: ASGIApp, *, default_limit: int, upload_limit: int, upload_prefix: str) -> None:
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit
        self.upload_prefix = upload_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = (
            self.upload_limit + 64 * 1024
            if scope["path"].startswith(self.upload_prefix)
            else self.default_limit
        )
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _reject(send, limit)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await _reject(send, limit)


class _TooLarge(Exception):
    pass


async def _reject(send: Send, limit: int) -> None:
    body = (
        b'{"error":{"code":"request_too_large","message":"request body exceeds '
        + str(limit).encode()
        + b' bytes"}}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
