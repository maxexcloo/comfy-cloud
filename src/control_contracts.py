from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionView(BaseModel):
    confirmation: str | None = None
    name: str


class Pagination(BaseModel):
    count: int
    page: int
    pages: int


class ProviderView(BaseModel):
    model_config = ConfigDict(extra="allow")

    actions: list[ActionView]
    active_requests: int
    configured: bool
    details: dict[str, Any]
    error: str | None = None
    id: str
    idle_seconds: int
    models: list[str]
    panel_url: str | None = None
    platform: str | None = None
    resource_id: str | None = None
    state: str
    type: str
    usage: dict[str, Any]


class OperationsStatus(BaseModel):
    event_pagination: Pagination
    events: list[dict[str, Any]]
    history: list[dict[str, Any]]
    history_pagination: Pagination
    jobs: list[dict[str, Any]]
    providers: list[ProviderView]


class ProviderLogs(BaseModel):
    entries: list[dict[str, Any]]
    provider: str
    source: str


class PreferenceDescription(BaseModel):
    model_config = ConfigDict(extra="allow")

    fields: list[dict[str, Any]]
    revision: int


class PreferenceUpdate(BaseModel):
    revision: int
    values: dict[str, Any]


class HistoryPage(BaseModel):
    data: list[dict[str, Any]]
    pagination: Pagination


class MediaSearch(BaseModel):
    count: int
    data: list[dict[str, Any]]


class MediaLineage(BaseModel):
    derivatives: list[dict[str, Any]]
    sources: list[dict[str, Any]]


class ProviderActionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    provider: str
    state: str


class ProviderTestRequest(BaseModel):
    model: str
    prompt: str = Field(min_length=1, max_length=2000)
    size: Literal["512x512", "768x768", "1024x1024"] = "512x512"


class ProviderTestResult(BaseModel):
    duration_seconds: float
    history_id: str
    media: list[dict[str, Any]]
    model: str
    provider: str
    status: Literal["completed"]
