from __future__ import annotations

import time
from importlib import resources
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import DictLoader, Environment, select_autoescape

from comfy_control.control.dashboard.auth import SESSION_SECONDS, csrf_token

TEMPLATE_NAMES = (
    "dashboard_base.html",
    "dashboard_components.html",
    "dashboard_events.html",
    "dashboard_history.html",
    "dashboard_log_dialog.html",
    "dashboard_navigation.html",
    "dashboard_providers.html",
    "dashboard_settings.html",
    "dashboard_settings_fields.html",
    "media_library.html",
)

TEMPLATES = Environment(
    autoescape=select_autoescape(("html", "xml")),
    loader=DictLoader(
        {
            name: resources.files("comfy_control.control.dashboard")
            .joinpath("templates", name)
            .read_text()
            for name in TEMPLATE_NAMES
        }
    ),
)


def pagination_context(
    request: Request, result: dict[str, object], page: int, page_size: int
) -> dict[str, object]:
    query = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "page"
    ]
    count = int(result["count"])
    return {
        "items": result["data"],
        "page": page,
        "pages": max(1, (count + page_size - 1) // page_size),
        "query_suffix": f"&{urlencode(query)}" if query else "",
    }


def render_dashboard(
    request: Request, template_name: str, page: str, **context: object
) -> HTMLResponse:
    settings = request.app.state.settings
    controller = request.app.state.controller
    return HTMLResponse(
        TEMPLATES.get_template(template_name).render(
            csrf_token=csrf_token(settings, int(time.time()) + SESSION_SECONDS),
            message=request.query_params.get("message", ""),
            page=page,
            time_zone=controller.preferences.display_time_zone,
            **context,
        ),
        headers={"Cache-Control": "no-store"},
    )
