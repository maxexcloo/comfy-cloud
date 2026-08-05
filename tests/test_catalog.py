from pathlib import Path

from comfy_cloud.catalog import Catalog

ROOT = Path(__file__).parents[1]


def test_catalog_loads_and_renders_workflow():
    catalog = Catalog.load((ROOT / "catalog",))
    model = catalog.get("example")
    graph = model.render({"prompt": "a red fox", "width": 768, "seed": 42})

    assert model.id == "example/checkpoint-text-to-image"
    assert graph["1"]["inputs"]["text"] == "a red fox"
    assert graph["2"]["inputs"]["width"] == 768
    assert graph["3"]["inputs"]["seed"] == 42


def test_catalog_maps_one_parameter_to_multiple_nodes():
    catalog = Catalog.load((ROOT / "catalog",))
    graph = catalog.get("flux-2-klein-4b").render({"height": 768, "width": 1344})

    assert graph["6"]["inputs"]["height"] == 768
    assert graph["6"]["inputs"]["width"] == 1344
    assert graph["9"]["inputs"]["height"] == 768
    assert graph["9"]["inputs"]["width"] == 1344


def test_catalog_exposes_alias_only_once():
    catalog = Catalog.load((ROOT / "catalog",))
    assert [model.id for model in catalog.list()] == [
        "example/checkpoint-text-to-image",
        "flux-2-klein-4b/text-to-image",
        "flux-2-klein-9b/text-to-image",
        "krea-2-turbo/text-to-image",
        "minimax-h3/text-to-video",
    ]


def test_catalog_only_exposes_workflows_with_required_models(tmp_path):
    catalog = Catalog.load((ROOT / "catalog",))
    model = catalog.get("example")
    model.required_files = ["checkpoints/example.safetensors"]

    assert model.missing_files(tmp_path) == ["checkpoints/example.safetensors"]
    assert catalog.list_available(tmp_path) == []

    checkpoint = tmp_path / "checkpoints/example.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"model")

    assert catalog.list_available(tmp_path) == [model]
