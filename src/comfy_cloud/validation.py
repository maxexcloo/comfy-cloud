from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from .catalogue import Catalogue


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


def validate_repository(catalogue_dir: Path, profiles_dir: Path) -> None:
    catalogue = Catalogue.load((catalogue_dir,))
    profiles: dict[str, dict[str, Any]] = {}
    for path in sorted(profiles_dir.glob("*.yaml")):
        profile = yaml.safe_load(path.read_text())
        if not isinstance(profile, dict):
            raise TypeError(f"profile must contain a mapping: {path}")
        name = profile.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"profile must have a name: {path}")
        if name in profiles:
            raise ValueError(f"duplicate profile name: {name}")
        if profile.get("minimum_vram_gb", 0) <= 0:
            raise ValueError(f"profile must declare positive minimum_vram_gb: {name}")
        if not isinstance(profile.get("sources"), list) or not profile["sources"]:
            raise ValueError(f"profile must declare sources: {name}")
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
        if model.profile == "example":
            continue
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
