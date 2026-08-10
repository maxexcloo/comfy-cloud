from pathlib import Path

from comfy_control.cli import create_parser

ROOT = Path(__file__).parents[1]


def test_runtime_services_use_the_packaged_command():
    parser = create_parser()

    assert parser.parse_args(["controller"]).func.__name__ == "controller"
    assert parser.parse_args(["worker"]).func.__name__ == "worker"
    assert (
        'CMD ["comfy-control", "controller"]'
        in (ROOT / "Dockerfile.control").read_text()
    )
    assert 'CMD ["comfy-control", "worker"]' in (ROOT / "Dockerfile").read_text()


def test_gateway_check_is_a_cli_command():
    assert (
        create_parser().parse_args(["gateway-check", "provider/model"]).func.__name__
        == "gateway_check"
    )
