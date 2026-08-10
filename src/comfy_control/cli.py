from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import uvicorn
import yaml

from .catalogue import Catalogue, WorkflowModel
from .fetch import fetch_profile
from .gateway_check import run as run_gateway_check
from .model_pack import pack_file, unpack_file
from .supervisor import run as run_worker
from .validation import validate_repository


def workflow_add(args: argparse.Namespace) -> None:
    destination = Path(args.catalogue_dir) / args.id.replace("/", "__")
    destination.mkdir(parents=True, exist_ok=True)
    workflow = Path(args.workflow)
    mapping = yaml.safe_load(Path(args.mapping).read_text())
    workflow_target = destination / "workflow.json"
    shutil.copy2(workflow, workflow_target)
    manifest = {
        **mapping,
        "id": args.id,
        "profile": args.profile or args.id.split("/", 1)[0],
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
    validate_repository(Path(args.catalogue_dir), Path(args.profiles_dir))
    print("catalogue workflows and model profiles are consistent")


def controller(args: argparse.Namespace) -> None:
    uvicorn.run(
        "comfy_control.control:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )


def gateway_check(args: argparse.Namespace) -> None:
    run_gateway_check(args.model, args.url)


def worker(args: argparse.Namespace) -> None:
    run_worker(args.host, args.port)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comfy-control")
    sub = parser.add_subparsers(required=True)
    listing = sub.add_parser(
        "catalogue-list", help="validate and list registered workflows"
    )
    listing.add_argument("--catalogue-dir", default="catalogue")
    listing.add_argument("--models-dir", default="models")
    listing.set_defaults(func=catalogue_list)
    control = sub.add_parser("controller", help="run the controller service")
    control.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    control.add_argument("--port", default=int(os.getenv("PORT", "8000")), type=int)
    control.set_defaults(func=controller)
    gateway = sub.add_parser("gateway-check", help="probe the configured Bifrost")
    gateway.add_argument("model")
    gateway.add_argument("--url", default="http://localhost:28080")
    gateway.set_defaults(func=gateway_check)
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
    check.add_argument("--profiles-dir", default="profiles")
    check.set_defaults(func=repository_check)
    unpack = sub.add_parser("unpack", help="verify and reconstruct a model pack")
    unpack.add_argument("manifest")
    unpack.add_argument("destination")
    unpack.set_defaults(func=unpack_model)
    worker_service = sub.add_parser("worker", help="run ComfyUI and its gateway")
    worker_service.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    worker_service.add_argument(
        "--port", default=int(os.getenv("PORT", "8000")), type=int
    )
    worker_service.set_defaults(func=worker)
    add = sub.add_parser("workflow-add", help="register an API-format workflow")
    add.add_argument("--id", required=True)
    add.add_argument("--profile")
    add.add_argument(
        "--operation",
        required=True,
        choices=["image_generation", "image_edit", "video_generation"],
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
