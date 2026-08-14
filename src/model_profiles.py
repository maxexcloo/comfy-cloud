from __future__ import annotations

import os
from pathlib import Path

import yaml


def profile_path(package: str) -> Path | None:
    profiles_dir = Path(os.getenv("PROFILES_DIR", "/opt/comfy-control/profiles"))
    candidates = (
        profiles_dir / f"{package}.yaml",
        Path("profiles") / f"{package}.yaml",
    )
    return next((path for path in candidates if path.is_file()), None)


def profile_details(package: str) -> dict[str, object]:
    path = profile_path(package)
    if path is None:
        return {"assets": [], "minimum_vram_gb": None}
    profile = yaml.safe_load(path.read_text())
    if not isinstance(profile, dict):
        return {"assets": [], "minimum_vram_gb": None}
    assets = []
    for source in profile.get("sources", []):
        if not isinstance(source, dict):
            continue
        destination = str(source.get("destination") or "").strip("/")
        includes = source.get("include") or []
        if isinstance(includes, str):
            includes = [includes]
        for item in includes:
            relative = str(item).removeprefix("split_files/")
            assets.append("/".join(value for value in (destination, relative) if value))
        if source.get("type") == "civitai" and destination:
            assets.append(destination)
    minimum_vram = profile.get("minimum_vram_gb")
    return {
        "assets": sorted(set(assets)),
        "minimum_vram_gb": minimum_vram if isinstance(minimum_vram, int) else None,
    }


def required_vram_gb(packages: list[str]) -> int:
    requirements = [
        value
        for package in packages
        if isinstance((value := profile_details(package)["minimum_vram_gb"]), int)
    ]
    return max(requirements, default=0)
