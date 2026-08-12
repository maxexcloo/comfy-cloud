from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .catalogue import Operation


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    model: str
    operation: Operation
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("execution_id")
    @classmethod
    def validate_execution_id(cls, value: str) -> str:
        if (
            not value
            or len(value) > 128
            or any(
                character not in "-0123456789_abcdefghijklmnopqrstuvwxyz"
                for character in value
            )
        ):
            raise ValueError("execution_id must be a lowercase identifier")
        return value


class ExecutionOutput(BaseModel):
    index: int
    content_type: str
    filename: str
    url: str


class ExecutionResult(BaseModel):
    execution_id: str
    outputs: list[ExecutionOutput]
    status: Literal["completed"] = "completed"
