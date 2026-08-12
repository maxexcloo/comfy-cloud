from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse


class RequestBodyTooLarge(ValueError):
    pass


def exception_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"provider API returned HTTP {exc.response.status_code}"
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
