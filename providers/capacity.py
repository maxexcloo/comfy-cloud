from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from catalogue.profiles import ProfilePolicy


class CapacityOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    provider: str
    available: bool = True
    benchmarked_p95_seconds: float | None = Field(default=None, gt=0)
    cost_per_hour: float | None = Field(default=None, ge=0)
    gpu: str | None = None
    location: str | None = None
    memory_gb: float | None = Field(default=None, ge=0)
    mode: Literal["pod", "proxy", "serverless"]
    reliability: float | None = Field(default=None, ge=0, le=1)

    def compatible(self, minimum_vram_gb: int) -> bool:
        return self.memory_gb is None or self.memory_gb >= minimum_vram_gb

    def within_cost_limit(self, hourly_cost_limit: float) -> bool:
        return self.cost_per_hour is None or self.cost_per_hour <= hourly_cost_limit

    def proven(self, latency_target_seconds: float = 20) -> bool:
        return (
            self.benchmarked_p95_seconds is not None
            and self.benchmarked_p95_seconds <= latency_target_seconds
        )


def rank_capacity(
    offers: list[CapacityOffer],
    *,
    hourly_cost_limit: float,
    minimum_vram_gb: int,
    latency_target_seconds: float = 20,
) -> list[CapacityOffer]:
    eligible = [
        offer
        for offer in offers
        if offer.available
        and offer.compatible(minimum_vram_gb)
        and offer.within_cost_limit(hourly_cost_limit)
    ]
    return sorted(
        eligible,
        key=lambda offer: (
            not offer.proven(latency_target_seconds),
            offer.cost_per_hour if offer.cost_per_hour is not None else float("inf"),
            -(offer.reliability or 0),
            offer.label.casefold(),
        ),
    )


def baseline_p95_seconds(
    policy: ProfilePolicy, provider: str, gpu: str
) -> float | None:
    normalised_gpu = gpu.casefold()
    return next(
        (
            float(item["p95_seconds"])
            for item in policy.benchmarks
            if item.get("provider") == provider
            and str(item.get("gpu", "")).casefold() in normalised_gpu
        ),
        None,
    )
