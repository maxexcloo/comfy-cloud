from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

import httpx
import yaml


def _relative_path(value: Any, field: str) -> Path:
    path = Path(value or "")
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be relative to the models directory")
    return path


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


@contextmanager
def _fetch_lock(models_dir: Path):
    state_dir = models_dir / ".comfy-cloud"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "fetch.lock").open("a+") as lock:
        flock(lock, LOCK_EX)
        try:
            yield state_dir
        finally:
            flock(lock, LOCK_UN)


def _prepared_files(
    state_path: Path, profile_digest: str, models_dir: Path
) -> list[Path] | None:
    try:
        state = json.loads(state_path.read_text())
        if state.get("profile_digest") != profile_digest:
            return None
        prepared: list[Path] = []
        for item in state["files"]:
            relative = _relative_path(item["path"], "Prepared model path")
            path = models_dir / relative
            if not path.is_file() or path.stat().st_size != item["size"]:
                return None
            prepared.append(path)
        return prepared
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_state(
    state_path: Path, profile_digest: str, files: list[Path], models_dir: Path
) -> None:
    state = {
        "profile_digest": profile_digest,
        "files": [
            {
                "path": str(path.relative_to(models_dir)),
                "size": path.stat().st_size,
            }
            for path in sorted(files)
        ],
    }
    temporary = state_path.with_suffix(".json.download")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(state_path)


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
    destination_value = source.get("destination")
    destination = (
        _relative_path(destination_value, "Hugging Face destination")
        if destination_value
        else Path()
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
            temporary_target = target.with_suffix(target.suffix + ".download")
            try:
                shutil.copy2(item, temporary_target)
                temporary_target.replace(target)
            finally:
                temporary_target.unlink(missing_ok=True)
            copied.append(target)
        return copied


def _fetch_civitai(source: dict[str, Any], models_dir: Path) -> list[Path]:
    token = os.getenv("CIVITAI_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    version_id = source.get("version_id")
    destination = source.get("destination")
    if not version_id or not destination:
        raise ValueError("Civitai sources require version_id and destination")
    destination_path = _relative_path(destination, "Civitai destination")
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
        target = models_dir / destination_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".download")
        try:
            with client.stream("GET", selected["downloadUrl"]) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes(8 * 1024 * 1024):
                        output.write(chunk)
            _verify(temporary, source.get("sha256"))
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return [target]


def fetch_profile(profile_path: Path, models_dir: Path) -> list[Path]:
    profile_text = profile_path.read_text()
    profile = yaml.safe_load(profile_text)
    if not isinstance(profile, dict):
        raise TypeError("profile must contain a mapping")
    profile_digest = hashlib.sha256(profile_text.encode()).hexdigest()
    profile_name = str(profile.get("name") or profile_path.stem)
    if not profile_name or any(
        character not in "-0123456789_abcdefghijklmnopqrstuvwxyz"
        for character in profile_name
    ):
        raise ValueError(
            "profile name may contain only lowercase letters, numbers, hyphens and underscores"
        )
    models_dir.mkdir(parents=True, exist_ok=True)
    with _fetch_lock(models_dir) as state_dir:
        state_path = state_dir / f"{profile_name}.json"
        prepared = _prepared_files(state_path, profile_digest, models_dir)
        if prepared is not None:
            return prepared
        downloaded: list[Path] = []
        for source in profile.get("sources", []):
            if source.get("type") == "huggingface":
                downloaded.extend(_fetch_huggingface(source, models_dir))
            elif source.get("type") == "civitai":
                downloaded.extend(_fetch_civitai(source, models_dir))
            else:
                raise ValueError(f"unsupported source type: {source.get('type')}")
        _write_state(state_path, profile_digest, downloaded, models_dir)
        return downloaded
