from pathlib import Path

from comfy_control.cli import create_parser

ROOT = Path(__file__).parents[1]


def test_runtime_service_uses_the_packaged_command():
    parser = create_parser()

    assert parser.parse_args(["worker"]).func.__name__ == "worker"
    assert 'CMD ["comfy-control", "worker"]' in (ROOT / "Dockerfile").read_text()
