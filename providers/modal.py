from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx

from control.config import ControlSettings, Provider
from control.preferences import ControlPreferences
from providers.base import BaseAdapter, Discovery, ProviderNotDeployed
from providers.deployment.common import worker_model_profiles


def action_response(status: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": status},
        request=httpx.Request("POST", "https://modal.invalid/internal"),
    )


def provider_action(
    action: str,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> httpx.Response:
    management = provider.management
    if management is None or management.kind != "modal":
        raise RuntimeError("Modal action requires Modal provider management")
    if action == "modal-terminate":
        subprocess.run(
            ["modal", "app", "stop", management.name, "--yes"],
            capture_output=True,
            check=True,
            text=True,
        )
        return action_response("terminated")

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
    environment["CONTROL_UI_PASSWORD"] = settings.ui_password
    environment["CONTROL_UI_USERNAME"] = settings.ui_username
    environment["MODEL_PROFILES"] = ",".join(
        worker_model_profiles(provider, preferences)
    )
    module.build_app(environment).deploy(name=management.name)
    return action_response("deployed")


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
        metrics.append({"label": "Credits applied", "unit": "USD", "value": credit})
    return metrics


def web_url(app_name: str, function_name: str) -> str:
    import modal

    url = modal.Function.from_name(app_name, function_name).get_web_url()
    if not url:
        raise RuntimeError(f"Modal web function has no URL: {app_name}/{function_name}")
    return url.rstrip("/")


class ModalAdapter(BaseAdapter):
    async def discover(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str | None,
        *,
        route: bool,
    ) -> Discovery:
        del client, preferences, route
        management = provider.management
        assert management is not None
        try:
            base_url = await asyncio.to_thread(
                web_url, management.name, management.function or ""
            )
        except Exception as exc:
            import modal

            if resource_id is None or isinstance(exc, modal.exception.NotFoundError):
                raise ProviderNotDeployed(
                    f"provider resource not found: {management.name}"
                ) from exc
            raise
        return Discovery(base_url, {}, management.name)

    async def live_status(self, provider: Provider) -> tuple[str, dict[str, object]]:
        management = provider.management
        assert management is not None
        details = await asyncio.to_thread(
            status, management.name, management.function or ""
        )
        return str(details.pop("state")), details

    def panel_url(
        self, provider: Provider, details: dict[str, object], base_url: str | None
    ) -> str | None:
        return str(details.get("panel_url") or self.panel)

    async def usage(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        history_usage: Callable[[str], dict[str, int]],
    ) -> list[dict[str, object]]:
        del client, provider, preferences, history_usage
        return await asyncio.to_thread(usage)
