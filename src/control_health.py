from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


@router.get("/health")
async def health(request: Request) -> dict[str, int | str]:
    controller = request.app.state.controller
    return {
        "status": "ready",
        "models": len(controller.config.models),
        "providers": len(controller.providers),
    }
