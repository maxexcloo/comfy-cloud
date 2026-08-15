from __future__ import annotations

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from catalogue.profiles import profile_policy
from control.dashboard.auth import ui_authorised, valid_csrf
from control.dashboard.rendering import render_dashboard
from control.http import error
from control.registry import MODEL_ALIASES

router = APIRouter(tags=["dashboard"])


def studio_catalogue(request: Request) -> list[dict[str, object]]:
    installed = set(request.app.state.controller.preferences.model_profiles)
    models = []
    for (profile, operation), aliases in sorted(MODEL_ALIASES.items()):
        if profile not in installed or operation not in {
            "image-generation",
            "text-to-video",
        }:
            continue
        kind = "image" if operation == "image-generation" else "video"
        policy = profile_policy(profile)
        for identifier in aliases:
            models.append(
                {
                    "description": policy.description,
                    "id": identifier,
                    "kind": kind,
                    "speed": (
                        "Warm target <20s"
                        if policy.service_class == "interactive"
                        else "Batch workload"
                    ),
                }
            )
    return sorted(models, key=lambda item: str(item["id"]))


@router.get("/", include_in_schema=False)
async def home(request: Request) -> Response:
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/generate", status_code=303)


@router.get("/generate", include_in_schema=False)
async def generate(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    models = studio_catalogue(request)
    providers = sorted(
        {
            target.provider
            for model in controller.config.models
            if model.id in {str(item["id"]) for item in models}
            for target in model.targets
        }
    )
    return render_dashboard(
        request,
        "generate.html",
        "generate",
        models=models,
        providers=providers,
    )


@router.post("/generate", include_in_schema=False)
async def create_generation(request: Request) -> Response:
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    form = await request.form()
    if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
        return error("invalid CSRF token", 403, "invalid_csrf")
    model = str(form.get("model", ""))
    details = next(
        (item for item in studio_catalogue(request) if item["id"] == model), None
    )
    if details is None:
        return error("model is not available", 400, "invalid_model")
    prompt = str(form.get("prompt", "")).strip()
    if not prompt:
        return error("prompt is required", 400, "invalid_request")
    kind = str(details["kind"])
    job_id = f"image_{uuid.uuid4().hex}" if kind == "image" else uuid.uuid4().hex
    payload: dict[str, object] = {"model": model, "prompt": prompt}
    if provider := str(form.get("provider", "")).strip():
        payload["provider"] = provider
    try:
        if kind == "image":
            payload["n"] = int(str(form.get("n", "1")))
            payload["response_format"] = "url"
            payload["size"] = str(form.get("size", "1024x1024"))
        if seed := str(form.get("seed", "")).strip():
            payload["seed"] = int(seed)
    except ValueError:
        return error("numeric options are invalid", 400, "invalid_request")
    generation = request.app.state.studio.submit(
        job_id,
        kind,
        model,
        payload,
        job_id if kind == "image" else None,
    )
    return render_dashboard(
        request,
        "generate_status.html",
        "generate",
        fragment=True,
        job=asdict(generation),
    )


@router.get("/generate/status/{job_id}", include_in_schema=False)
async def generation_status(job_id: str, request: Request) -> Response:
    if not ui_authorised(request, request.app.state.settings):
        return Response(status_code=401)
    store = request.app.state.controller.store
    generation = store.generation_request(job_id)
    if generation is None:
        return error("generation was not found", 404, "not_found")
    job = asdict(generation)
    history = store.history(generation.history_id) if generation.history_id else None
    if history is not None:
        job["status"] = history["status"]
        job["error"] = history["error"]
        if history["status"] == "completed":
            media = store.media_library(
                filters=[{"path": "history_id", "value": generation.history_id}]
            )["data"]
            job["outputs"] = [item for item in media if item["role"] == "output"]
    return render_dashboard(
        request,
        "generate_status.html",
        "generate",
        fragment=True,
        history=history,
        job=job,
    )
