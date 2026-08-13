from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse


class RequestBodyTooLarge(ValueError):
    pass


def provider_error_detail(response: httpx.Response) -> str | None:
    try:
        value = response.json()
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    for name in ("message", "detail", "error"):
        candidate = value.get(name)
        if isinstance(candidate, dict):
            candidate = candidate.get("message")
        if isinstance(candidate, str) and candidate.strip():
            return " ".join(candidate.split())[:300]
    return None


def exception_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        message = f"provider API returned HTTP {exc.response.status_code}"
        if detail := provider_error_detail(exc.response):
            message = f"{message}: {detail}"
        return message
    if isinstance(exc, httpx.RequestError):
        return f"provider connection failed ({type(exc).__name__})"
    return str(exc)


def error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "type": "server_error" if status >= 500 else "invalid_request_error",
            }
        },
    )


async def limited_body(request: Request, maximum_bytes: int) -> bytes:
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > maximum_bytes:
        raise RequestBodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise RequestBodyTooLarge
    request._body = bytes(body)  # Starlette form parsing reuses the bounded body.
    return request._body
