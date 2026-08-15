from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from control.config import ControlSettings
from control.dashboard.activity import router as dashboard_activity_router
from control.dashboard.generate import router as dashboard_generate_router
from control.dashboard.media import router as dashboard_media_router
from control.dashboard.routes import router as dashboard_router
from control.dashboard.sessions import router as dashboard_sessions_router
from control.dashboard.settings import router as dashboard_settings_router
from control.health import router as health_router
from control.inference.images import router as inference_images_router
from control.inference.models import router as inference_models_router
from control.inference.videos import router as inference_videos_router
from control.operations.history import router as history_operations_router
from control.operations.media import router as media_operations_router
from control.operations.models import router as model_operations_router
from control.operations.providers import (
    router as provider_operations_router,
)
from control.operations.routes import router as route_operations_router
from control.operations.settings import (
    router as settings_operations_router,
)
from control.operations.status import router as status_operations_router
from control.service import Controller
from control.studio import StudioRunner


def create_app(settings: ControlSettings | None = None) -> FastAPI:
    settings = settings or ControlSettings.from_env()
    controller = Controller(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for pending_job in controller.store.pending_jobs():
            controller.start_video(pending_job)
        reaper = asyncio.create_task(controller.idle_reaper())
        app.state.studio.start()
        yield
        await app.state.studio.close()
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        await controller.close()

    app = FastAPI(title="Comfy Control", version="current", lifespan=lifespan)
    app.state.controller = controller
    app.state.settings = settings
    app.state.studio = StudioRunner(app)
    app.include_router(dashboard_activity_router)
    app.include_router(dashboard_generate_router)
    app.include_router(dashboard_media_router)
    app.include_router(dashboard_router)
    app.include_router(dashboard_sessions_router)
    app.include_router(dashboard_settings_router)
    app.include_router(health_router)
    app.include_router(history_operations_router)
    app.include_router(inference_models_router)
    app.include_router(inference_images_router)
    app.include_router(inference_videos_router)
    app.include_router(media_operations_router)
    app.include_router(model_operations_router)
    app.include_router(provider_operations_router)
    app.include_router(route_operations_router)
    app.include_router(settings_operations_router)
    app.include_router(status_operations_router)

    def current_openapi() -> dict[str, object]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=(
                    "Current OpenAI-compatible inference and Comfy Control "
                    "operations contract."
                ),
                routes=app.routes,
            )
            schema["paths"] = {
                path: value
                for path, value in schema["paths"].items()
                if path.startswith(("/ops/", "/v1/"))
            }
            components = schema.setdefault("components", {})
            components.setdefault("securitySchemes", {})["bearerAuth"] = {
                "scheme": "bearer",
                "type": "http",
            }
            for value in schema["paths"].values():
                for operation in value.values():
                    operation["security"] = [{"bearerAuth": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = current_openapi  # type: ignore[method-assign]
    return app
