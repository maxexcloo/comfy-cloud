from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from control.config import Target
from providers.economics import rank_targets

if TYPE_CHECKING:
    from control.service import Controller, ProviderRuntime


class CapacityManager:
    def __init__(self, controller: Controller):
        self.controller = controller

    async def economical_targets(self, targets: list[Target]) -> list[Target]:
        runtimes = [self.controller.providers[target.provider] for target in targets]
        checks = [self.refresh_cost(runtime) for runtime in runtimes]
        modal = self.controller.providers.get("modal")
        if modal is not None and any(target.provider == "modal" for target in targets):
            checks.append(self.controller.bounded_provider_usage(modal))
        await asyncio.gather(*checks, return_exceptions=True)
        return rank_targets(
            targets,
            {runtime.config.id: runtime.capacity_cost_per_hour for runtime in runtimes},
            {runtime.config.id: runtime.usage for runtime in runtimes},
        )

    async def refresh_cost(self, runtime: ProviderRuntime) -> None:
        if time.monotonic() - runtime.capacity_checked_at < 300:
            return
        runtime.capacity_checked_at = time.monotonic()
        try:
            options = await asyncio.wait_for(
                self.controller.deployment_options(runtime.config.id), timeout=5
            )
        except Exception:  # noqa: BLE001 - optional ranking evidence
            return
        runtime.capacity_cost_per_hour = next(
            (
                float(option["cost_per_hour"])
                for option in options
                if option.get("available", True)
                and option.get("compatible", True)
                and option.get("within_cost_limit", True)
                and isinstance(option.get("cost_per_hour"), (float, int))
            ),
            None,
        )
