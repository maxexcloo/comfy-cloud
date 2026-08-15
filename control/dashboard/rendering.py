from __future__ import annotations

import time
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, select_autoescape

from control.dashboard.auth import SESSION_SECONDS, csrf_token

TEMPLATES = Environment(
    autoescape=select_autoescape(("html", "xml")),
    loader=PackageLoader("control.dashboard", "templates"),
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
    pages = max(1, (count + page_size - 1) // page_size)
    visible = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    links: list[int | None] = []
    for number in visible:
        if links and links[-1] is not None and number - int(links[-1]) > 1:
            links.append(None)
        links.append(number)
    return {
        "count": count,
        "items": result["data"],
        "links": links,
        "page": page,
        "pages": pages,
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
