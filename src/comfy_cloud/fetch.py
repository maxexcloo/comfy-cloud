from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
import yaml


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _verify(path: Path, expected: str | None) -> None:
    if expected and _sha256(path) != expected.lower():
        path.unlink(missing_ok=True)
        raise ValueError(f"checksum mismatch: {path}")


def _fetch_huggingface(source: dict[str, Any], models_dir: Path) -> list[Path]:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "install comfy-cloud[build] to fetch Hugging Face profiles"
        ) from exc
    revision = source.get("revision")
    if not revision:
        raise ValueError("Hugging Face sources must pin revision")
    destination = Path(source.get("destination", ""))
    if destination.is_absolute() or ".." in destination.parts:
        raise ValueError(
            "Hugging Face destination must be relative to the models directory"
        )
    with tempfile.TemporaryDirectory(prefix="comfy-cloud-hf-") as temporary:
        root = Path(
            snapshot_download(
                allow_patterns=source.get("include"),
                local_dir=temporary,
                repo_id=source["repo"],
                revision=revision,
                token=os.getenv("HF_TOKEN"),
            )
        )
        copied: list[Path] = []
        for item in root.rglob("*"):
            if not item.is_file() or ".cache" in item.parts:
                continue
            relative = item.relative_to(root)
            if relative.parts and relative.parts[0] == "split_files":
                relative = Path(*relative.parts[1:])
            target = models_dir / destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied.append(target)
        return copied


def _fetch_civitai(source: dict[str, Any], models_dir: Path) -> list[Path]:
    token = os.getenv("CIVITAI_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    version_id = source.get("version_id")
    destination = source.get("destination")
    if not version_id or not destination:
        raise ValueError("Civitai sources require version_id and destination")
    with httpx.Client(timeout=None, follow_redirects=True, headers=headers) as client:
        metadata = client.get(f"https://civitai.com/api/v1/model-versions/{version_id}")
        metadata.raise_for_status()
        files = metadata.json().get("files", [])
        requested = source.get("filename")
        selected = (
            next((item for item in files if item.get("name") == requested), None)
            if requested
            else (files[0] if files else None)
        )
        if not selected:
            raise ValueError(
                f"Civitai version {version_id} did not contain the requested file"
            )
        target = models_dir / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        with client.stream("GET", selected["downloadUrl"]) as response:
            response.raise_for_status()
            with target.open("wb") as output:
                for chunk in response.iter_bytes(8 * 1024 * 1024):
                    output.write(chunk)
    _verify(target, source.get("sha256"))
    return [target]


def fetch_profile(profile_path: Path, models_dir: Path) -> list[Path]:
    profile = yaml.safe_load(profile_path.read_text())
    downloaded: list[Path] = []
    for source in profile.get("sources", []):
        if source.get("type") == "huggingface":
            downloaded.extend(_fetch_huggingface(source, models_dir))
        elif source.get("type") == "civitai":
            downloaded.extend(_fetch_civitai(source, models_dir))
        else:
            raise ValueError(f"unsupported source type: {source.get('type')}")
    return downloaded
