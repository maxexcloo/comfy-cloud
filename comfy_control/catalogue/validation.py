from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from comfy_control.catalogue.workflows import Catalogue, workflow_operation_names


def _source_patterns(source: dict[str, Any]) -> list[str]:
    if source.get("type") == "civitai":
        return [str(source.get("destination", ""))]
    if source.get("type") != "huggingface":
        return []
    include = source.get("include", [])
    if isinstance(include, str):
        include = [include]
    destination = str(source.get("destination", "")).strip("/")
    patterns = []
    for value in include:
        pattern = str(value)
        if pattern.startswith("split_files/"):
            pattern = pattern.removeprefix("split_files/")
        if destination:
            pattern = f"{destination}/{pattern}"
        patterns.append(pattern)
    return patterns


def validate_repository(catalogue_dir: Path) -> None:
    catalogue = Catalogue.load((catalogue_dir,))
    profiles: dict[str, dict[str, Any]] = {}
    for path in sorted((catalogue_dir / "profiles").glob("*.yaml")):
        profile = yaml.safe_load(path.read_text())
        if not isinstance(profile, dict):
            raise TypeError(f"profile must contain a mapping: {path}")
        name = profile.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"profile must have a name: {path}")
        if name in profiles:
            raise ValueError(f"duplicate profile name: {name}")
        minimum_vram = profile.get("minimum_vram_gb")
        if not isinstance(minimum_vram, int) or minimum_vram < 0:
            raise ValueError(
                f"profile must declare non-negative minimum_vram_gb: {name}"
            )
        if not isinstance(profile.get("sources"), list):
            raise TypeError(f"profile must declare a source list: {name}")
        if minimum_vram > 0 and not profile["sources"]:
            raise ValueError(f"model profile must declare sources: {name}")
        if path.stem != name:
            raise ValueError(f"profile name must match its filename: {name}")
        for source in profile["sources"]:
            if not isinstance(source, dict):
                raise TypeError(f"profile source must be a mapping: {name}")
            if source.get("type") == "huggingface":
                revision = source.get("revision", "")
                if len(revision) != 40 or any(
                    character not in "0123456789abcdef" for character in revision
                ):
                    raise ValueError(
                        f"Hugging Face revision must be a full commit: {name}"
                    )
            elif source.get("type") == "civitai":
                if not source.get("version_id"):
                    raise ValueError(f"Civitai source must pin version_id: {name}")
            else:
                raise ValueError(f"unsupported profile source type: {name}")
        profiles[name] = profile

    referenced_profiles = {model.profile for model in catalogue.list()}
    for model in catalogue.list():
        if not model.workflow_sha256:
            raise ValueError(f"workflow must declare workflow_sha256: {model.id}")
        operation_name, operation_suffix = workflow_operation_names(
            model.operation, "image" in model.input_map
        )
        implementation = (
            model.id.rsplit("/", 1)[-1]
            if model.operation == "image_upscale"
            else operation_name
        )
        expected_id = f"{model.profile}/{implementation}"
        if model.id != expected_id:
            raise ValueError(f"workflow id must be {expected_id}: {model.id}")
        expected_directory = (
            f"{model.profile}-{implementation}"
            if model.operation == "image_upscale"
            else f"{model.profile}-{operation_suffix}"
        )
        workflow_path = model._workflow_path
        if workflow_path is None or workflow_path.parent.name != expected_directory:
            raise ValueError(
                f"workflow directory must be {expected_directory}: {model.id}"
            )
        try:
            profile = profiles[model.profile]
        except KeyError as exc:
            raise ValueError(
                f"unknown profile for {model.id}: {model.profile}"
            ) from exc
        patterns = [
            pattern
            for source in profile["sources"]
            for pattern in _source_patterns(source)
        ]
        for required in model.required_files:
            if not any(fnmatch(required, pattern) for pattern in patterns):
                raise ValueError(
                    f"{model.id}: required file is not supplied by profile "
                    f"{model.profile}: {required}"
                )

    for name, profile in profiles.items():
        if name not in referenced_profiles and profile.get("catalogued", True):
            raise ValueError(f"profile has no catalogue workflow: {name}")
