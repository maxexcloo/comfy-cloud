from __future__ import annotations

from collections.abc import Mapping

from control.config import Target

MONTHLY_CREDIT_USD = {"modal": 30.0}


def usage_value(usage: object, label: str) -> float | None:
    if not isinstance(usage, dict) or not isinstance(usage.get("metrics"), list):
        return None
    return next(
        (
            float(metric["value"])
            for metric in usage["metrics"]
            if isinstance(metric, dict)
            and metric.get("label") == label
            and isinstance(metric.get("value"), (float, int))
        ),
        None,
    )


def rank_targets(
    targets: list[Target],
    hourly_costs: Mapping[str, float | None],
    provider_usage: Mapping[str, object],
) -> list[Target]:
    position = {target.provider: index for index, target in enumerate(targets)}

    def key(target: Target) -> tuple[float, float, int]:
        included = MONTHLY_CREDIT_USD.get(target.provider, 0)
        metered = usage_value(provider_usage.get(target.provider), "Metered")
        if included and metered is not None and metered < included:
            return (0, 0, position[target.provider])
        cost = hourly_costs.get(target.provider)
        if cost is not None:
            return (1, cost, position[target.provider])
        return (2, 0, position[target.provider])

    return sorted(targets, key=key)
