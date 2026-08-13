from __future__ import annotations

import hashlib
import hmac
import time
from html import escape

from fastapi import Request

from .control_config import ControlSettings

SESSION_COOKIE = "comfy_control_session"
SESSION_SECONDS = 12 * 60 * 60


def bearer_authorised(request: Request, settings: ControlSettings) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value, settings.api_key)


def session_secret(settings: ControlSettings) -> bytes:
    return (settings.ui_password or settings.api_key).encode()


def session_token(settings: ControlSettings, expires: int) -> str:
    payload = str(expires)
    signature = hmac.new(
        session_secret(settings), payload.encode(), hashlib.sha512
    ).hexdigest()
    return f"{payload}.{signature}"


def csrf_token(settings: ControlSettings, expires: int) -> str:
    payload = f"csrf:{expires}"
    signature = hmac.new(
        session_secret(settings), payload.encode(), hashlib.sha512
    ).hexdigest()
    return f"{expires}.{signature}"


def valid_csrf(request: Request, settings: ControlSettings, token: str) -> bool:
    expires, separator, signature = token.partition(".")
    if not separator:
        return False
    try:
        expiry = int(expires)
    except ValueError:
        return False
    expected = csrf_token(settings, expiry).partition(".")[2]
    return (
        ui_authorised(request, settings)
        and expiry >= int(time.time())
        and hmac.compare_digest(signature, expected)
    )


def ui_authorised(request: Request, settings: ControlSettings) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload, separator, signature = token.partition(".")
    if not separator:
        return False
    try:
        expires = int(payload)
    except ValueError:
        return False
    expected = session_token(settings, expires).partition(".")[2]
    return expires >= int(time.time()) and hmac.compare_digest(signature, expected)


def secure_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").partition(",")[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def login_html(settings: ControlSettings, invalid: bool = False) -> str:
    message = (
        '<p class="error" role="alert">Incorrect Username Or Password.</p>'
        if invalid
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Sign In · Comfy Control</title><style>
:root{{color-scheme:dark;font:15px system-ui;background:#101418;color:#e8edf2}}
body{{display:grid;margin:0;min-height:100vh;place-items:center}}main{{background:#181e24;border:1px solid #2c3640;border-radius:10px;padding:2rem;width:min(320px,calc(100vw - 4rem))}}
h1{{font-size:1.5rem;margin-top:0}}label{{display:grid;gap:.35rem;margin:1rem 0}}input{{background:#101418;border:1px solid #42576b;border-radius:5px;color:#e8edf2;padding:.65rem}}
button{{background:#263442;border:1px solid #42576b;border-radius:5px;color:#e8edf2;padding:.65rem;width:100%;cursor:pointer}}.error{{color:#ff7b72}}
</style></head><body><main><h1>Comfy Control</h1>{message}<form method="post" action="/login">
<label>Username<input name="username" value="{escape(settings.ui_username)}" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required autofocus></label>
<button type="submit">Sign In</button></form></main></body></html>"""
