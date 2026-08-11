from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Operation = Literal["image_generation", "image_edit", "video_generation"]


class NodeTarget(BaseModel):
    node: str
    input: str


class OutputTarget(BaseModel):
    node: str
    type: Literal["image", "video", "audio"]


class WorkflowModel(BaseModel):
    id: str
    profile: str
    operation: Operation
    workflow: str
    workflow_sha256: str | None = None
    owned_by: str = "comfy-control"
    aliases: list[str] = Field(default_factory=list)
    input_map: dict[str, NodeTarget | list[NodeTarget]]
    output: OutputTarget
    defaults: dict[str, Any] = Field(default_factory=dict)
    required_files: list[str] = Field(default_factory=list)

    _workflow_path: Path | None = None
    _graph: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_id(self) -> WorkflowModel:
        if not self.id or self.id.startswith("/") or ".." in self.id:
            raise ValueError("model id must be a stable relative identifier")
        workflow = Path(self.workflow)
        if workflow.is_absolute() or ".." in workflow.parts:
            raise ValueError("workflow must be relative to its catalogue manifest")
        if "prompt" not in self.input_map:
            raise ValueError("input_map must include prompt")
        if self.operation == "image_edit" and "image" not in self.input_map:
            raise ValueError("image_edit input_map must include image")
        for required in self.required_files:
            path = Path(required)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    "required_files must be relative to the ComfyUI models directory"
                )
        return self

    def missing_files(self, models_dir: Path) -> list[str]:
        return [
            required
            for required in self.required_files
            if not (models_dir / required).is_file()
        ]

    def missing_nodes(self, object_info: dict[str, Any]) -> list[str]:
        if self._graph is None:
            raise RuntimeError("workflow model is not bound")
        return sorted(
            {node["class_type"] for node in self._graph.values()} - set(object_info)
        )

    def bind(self, manifest_path: Path) -> None:
        workflow_path = (manifest_path.parent / self.workflow).resolve()
        if not workflow_path.is_file():
            raise ValueError(f"workflow does not exist: {workflow_path}")
        payload = workflow_path.read_bytes()
        if (
            self.workflow_sha256
            and hashlib.sha256(payload).hexdigest() != self.workflow_sha256
        ):
            raise ValueError(f"workflow checksum mismatch: {workflow_path}")
        graph = json.loads(payload)
        if not isinstance(graph, dict) or not graph:
            raise ValueError(
                f"workflow must contain a non-empty graph: {workflow_path}"
            )
        for node_id, node in graph.items():
            if not isinstance(node, dict):
                raise TypeError(f"{self.id}: node {node_id} must be a mapping")
            if not isinstance(node.get("class_type"), str):
                raise TypeError(f"{self.id}: node {node_id} has no class_type")
            if not isinstance(node.get("inputs"), dict):
                raise TypeError(f"{self.id}: node {node_id} has no inputs")
        for name, configured_targets in self.input_map.items():
            targets = (
                configured_targets
                if isinstance(configured_targets, list)
                else [configured_targets]
            )
            for target in targets:
                if target.node not in graph:
                    raise ValueError(
                        f"{self.id}: mapping {name} references missing node {target.node}"
                    )
                if target.input not in graph[target.node]["inputs"]:
                    raise ValueError(
                        f"{self.id}: mapping {name} references missing input {target.input} on node {target.node}"
                    )
        if self.output.node not in graph:
            raise ValueError(
                f"{self.id}: output references missing node {self.output.node}"
            )
        self._workflow_path = workflow_path
        self._graph = graph

    def render(self, values: dict[str, Any]) -> dict[str, Any]:
        if self._graph is None:
            raise RuntimeError("workflow model is not bound")
        graph = copy.deepcopy(self._graph)
        merged = self.defaults | {
            key: value for key, value in values.items() if value is not None
        }
        for name, configured_targets in self.input_map.items():
            if name in merged:
                targets = (
                    configured_targets
                    if isinstance(configured_targets, list)
                    else [configured_targets]
                )
                for target in targets:
                    graph[target.node]["inputs"][target.input] = merged[name]
        return graph


class Catalogue:
    def __init__(self, models: list[WorkflowModel]):
        self._models: dict[str, WorkflowModel] = {}
        self._canonical: list[WorkflowModel] = []
        for model in models:
            if model.id in self._models:
                raise ValueError(f"duplicate model id: {model.id}")
            self._models[model.id] = model
            self._canonical.append(model)
            for alias in model.aliases:
                if alias in self._models:
                    raise ValueError(f"duplicate model alias: {alias}")
                self._models[alias] = model

    @classmethod
    def load(cls, roots: tuple[Path, ...]) -> Catalogue:
        models: list[WorkflowModel] = []
        for root in roots:
            if not root.exists():
                continue
            for manifest in sorted(root.glob("**/*.yaml")):
                raw = yaml.safe_load(manifest.read_text())
                if not raw or "id" not in raw:
                    continue
                model = WorkflowModel.model_validate(raw)
                model.bind(manifest)
                models.append(model)
        return cls(models)

    def list(self) -> list[WorkflowModel]:
        return list(self._canonical)

    def list_available(
        self, models_dir: Path, object_info: dict[str, Any] | None = None
    ) -> list[WorkflowModel]:
        return [
            model
            for model in self._canonical
            if not model.missing_files(models_dir)
            and (object_info is None or not model.missing_nodes(object_info))
        ]

    def get(self, model_id: str) -> WorkflowModel:
        try:
            return self._models[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model: {model_id}") from exc

    def get_available(
        self, model_id: str, models_dir: Path, object_info: dict[str, Any] | None = None
    ) -> WorkflowModel:
        model = self.get(model_id)
        if model.missing_files(models_dir):
            raise KeyError(f"model is not installed: {model_id}")
        if object_info is not None and model.missing_nodes(object_info):
            raise KeyError(f"model workflow uses unregistered nodes: {model_id}")
        return model
