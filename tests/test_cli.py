from pathlib import Path

from comfy_control.cli import create_parser

ROOT = Path(__file__).parents[1]


def test_runtime_images_use_the_packaged_commands():
    parser = create_parser()

    assert parser.parse_args(["control"]).func.__name__ == "control"
    assert parser.parse_args(["pod"]).func.__name__ == "pod"
    assert parser.parse_args(["serverless"]).func.__name__ == "serverless"
    assert (
        'CMD ["comfy-control", "control"]' in (ROOT / "Dockerfile.control").read_text()
    )
    assert 'CMD ["comfy-control", "pod"]' in (ROOT / "Dockerfile.worker").read_text()
