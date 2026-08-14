from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

from comfy_control.catalogue.fetch import fetch_profile
from comfy_control.catalogue.importer import import_grok_catalogue
from comfy_control.catalogue.pack import pack_file, unpack_file
from comfy_control.catalogue.validation import validate_repository
from comfy_control.catalogue.workflows import (
    Catalogue,
    WorkflowModel,
    workflow_operation_names,
)
from comfy_control.control.app import create_app as create_control_app
from comfy_control.control.config import ControlSettings
from comfy_control.worker.supervisor import run


def workflow_add(args: argparse.Namespace) -> None:
    workflow = Path(args.workflow)
    mapping = yaml.safe_load(Path(args.mapping).read_text())
    profile = args.profile or args.id.split("/", 1)[0]
    operation_name, operation_suffix = workflow_operation_names(
        args.operation, "image" in mapping.get("input_map", {})
    )
    expected_id = f"{profile}/{operation_name}"
    if args.id != expected_id:
        raise ValueError(f"workflow id must be {expected_id}")
    destination = Path(args.catalogue_dir) / f"{profile}-{operation_suffix}"
    destination.mkdir(parents=True, exist_ok=True)
    workflow_target = destination / "workflow.json"
    shutil.copy2(workflow, workflow_target)
    manifest = {
        **mapping,
        "id": args.id,
        "profile": profile,
        "operation": args.operation,
        "workflow": "workflow.json",
        "workflow_sha256": hashlib.sha256(workflow_target.read_bytes()).hexdigest(),
    }
    manifest_path = destination / "model.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    model = WorkflowModel.model_validate(manifest)
    model.bind(manifest_path)
    print(f"registered {model.id} in {destination}")


def catalogue_list(args: argparse.Namespace) -> None:
    catalogue = Catalogue.load((Path(args.catalogue_dir),))
    models_dir = Path(args.models_dir)
    print(
        json.dumps(
            [
                {
                    "id": model.id,
                    "operation": model.operation,
                    "available": not model.missing_files(models_dir),
                    "missing_files": model.missing_files(models_dir),
                }
                for model in catalogue.list()
            ],
            indent=2,
        )
    )


def catalogue_import(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            import_grok_catalogue(Path(args.source), Path(args.database)), indent=2
        )
    )


def pack_model(args: argparse.Namespace) -> None:
    manifest = pack_file(
        Path(args.source), Path(args.destination), args.chunk_size_gib * 1024**3
    )
    print(json.dumps(manifest, indent=2))


def unpack_model(args: argparse.Namespace) -> None:
    print(unpack_file(Path(args.manifest), Path(args.destination)))


def models_fetch(args: argparse.Namespace) -> None:
    files = fetch_profile(Path(args.profile), Path(args.models_dir))
    print(json.dumps([str(path) for path in files], indent=2))


def repository_check(args: argparse.Namespace) -> None:
    validate_repository(Path(args.catalogue_dir))
    print("catalogue workflows and model profiles are consistent")


def pod(args: argparse.Namespace) -> None:
    run("pod", args.host, args.port)


def serverless(args: argparse.Namespace) -> None:
    run("serverless", args.host, args.port)


def vast_serverless(_: argparse.Namespace) -> None:
    from comfy_control.worker import vast_gateway

    gateway = subprocess.Popen(["comfy-control", "serverless"])
    try:
        vast_gateway.main()
    finally:
        gateway.terminate()
        gateway.wait(timeout=30)


def control(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        create_control_app(ControlSettings.from_env()), host=args.host, port=args.port
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comfy-control")
    sub = parser.add_subparsers(required=True)
    listing = sub.add_parser(
        "catalogue-list", help="validate and list registered workflows"
    )
    listing.add_argument("--catalogue-dir", default="catalogue")
    listing.add_argument("--models-dir", default="models")
    listing.set_defaults(func=catalogue_list)
    catalogue_importer = sub.add_parser(
        "catalogue-import", help="import a Grok catalogue export"
    )
    catalogue_importer.add_argument("source")
    catalogue_importer.add_argument(
        "--database", default=os.getenv("CONTROL_DATABASE", "/data/comfy-control.db")
    )
    catalogue_importer.set_defaults(func=catalogue_import)
    fetch = sub.add_parser(
        "models-fetch", help="fetch a pinned Hugging Face/Civitai profile"
    )
    fetch.add_argument("profile")
    fetch.add_argument("--models-dir", default="models")
    fetch.set_defaults(func=models_fetch)
    pack = sub.add_parser("pack", help="split a model into GHCR-safe chunks")
    pack.add_argument("source")
    pack.add_argument("destination")
    pack.add_argument("--chunk-size-gib", type=int, default=8)
    pack.set_defaults(func=pack_model)
    check = sub.add_parser(
        "repository-check", help="validate bundled workflows and model profiles"
    )
    check.add_argument("--catalogue-dir", default="catalogue")
    check.set_defaults(func=repository_check)
    control_service = sub.add_parser("control", help="run the routing control plane")
    control_service.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    control_service.add_argument(
        "--port", default=int(os.getenv("PORT", "8000")), type=int
    )
    control_service.set_defaults(func=control)
    unpack = sub.add_parser("unpack", help="verify and reconstruct a model pack")
    unpack.add_argument("manifest")
    unpack.add_argument("destination")
    unpack.set_defaults(func=unpack_model)
    pod_service = sub.add_parser("pod", help="run ComfyUI with its authenticated UI")
    pod_service.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    pod_service.add_argument("--port", default=int(os.getenv("PORT", "8000")), type=int)
    pod_service.set_defaults(func=pod)
    serverless_service = sub.add_parser(
        "serverless", help="run ComfyUI without its frontend"
    )
    serverless_service.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    serverless_service.add_argument(
        "--port", default=int(os.getenv("PORT", "8000")), type=int
    )
    serverless_service.set_defaults(func=serverless)
    vast_service = sub.add_parser(
        "vast-serverless", help="run the gateway with Vast.ai serverless ingress"
    )
    vast_service.set_defaults(func=vast_serverless)
    add = sub.add_parser("workflow-add", help="register an API-format workflow")
    add.add_argument("--id", required=True)
    add.add_argument("--profile")
    add.add_argument(
        "--operation",
        required=True,
        choices=[
            "image_edit",
            "image_generation",
            "image_upscale",
            "video_generation",
        ],
    )
    add.add_argument("--workflow", required=True)
    add.add_argument("--mapping", required=True)
    add.add_argument("--catalogue-dir", default="catalogue/custom")
    add.set_defaults(func=workflow_add)
    return parser


def main() -> None:
    args = create_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
