from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import httpx

from .control_config import ControlSettings, Provider
from .control_preferences import ControlPreferences


def provider_action(
    action: str,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> httpx.Response:
    import modal

    management = provider.management
    if management is None or management.kind != "modal":
        raise RuntimeError("Modal action requires Modal provider management")
    if action == "modal-terminate":
        modal.App.lookup(management.name).stop()
        return httpx.Response(200, json={"status": "terminated"})
    path = Path(
        os.getenv("CONTROL_MODAL_APP", "/opt/comfy-control/deploy/modal/app.py")
    )
    if not path.is_file():
        raise RuntimeError(f"Modal deployment asset was not found: {path}")
    specification = importlib.util.spec_from_file_location("comfy_control_modal", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("Modal deployment asset could not be loaded")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    environment = preferences.environment()
    environment["API_KEY"] = preferences.worker_api_key
    environment["COMFY_UI_PASSWORD"] = (
        preferences.comfy_ui_password or settings.ui_password
    )
    environment["COMFY_UI_USERNAME"] = (
        preferences.comfy_ui_username or settings.ui_username
    )
    module.build_app(environment).deploy(name=management.name)
    return httpx.Response(200, json={"status": "deployed"})


def status(app_name: str, function_name: str) -> dict[str, object]:
    import modal

    function = modal.Function.from_name(app_name, function_name)
    stats = function.get_current_stats()
    values = {
        name: getattr(stats, name)
        for name in (
            "backlog",
            "num_active_runners",
            "num_total_runners",
        )
        if hasattr(stats, name)
    }
    active = values.get("num_active_runners") or values.get("num_total_runners")
    values["state"] = "ready" if active else "scaled-down"
    get_dashboard_url = getattr(function, "get_dashboard_url", None)
    if callable(get_dashboard_url):
        values["panel_url"] = get_dashboard_url()
    return values


def usage() -> list[dict[str, object]]:
    import modal

    summary = modal.Workspace.from_context().billing.summary()
    metrics = [
        {"label": "Billed", "unit": "USD", "value": float(summary.billed_cost)},
        {"label": "Metered", "unit": "USD", "value": float(summary.metered_cost)},
    ]
    credit = sum(
        abs(float(value))
        for key, value in summary.adjustments.items()
        if "credit" in key.lower()
    )
    if credit:
        metrics.append({"label": "Credits used", "unit": "USD", "value": credit})
    return metrics


def web_url(app_name: str, function_name: str) -> str:
    import modal

    url = modal.Function.from_name(app_name, function_name).get_web_url()
    if not url:
        raise RuntimeError(f"Modal web function has no URL: {app_name}/{function_name}")
    return url.rstrip("/")
