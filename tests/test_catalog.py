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


def test_catalog_maps_image_edit_inputs_to_reference_workflow():
    catalog = Catalog.load((ROOT / "catalog",))
    graph = catalog.get("flux-2-klein-4b-edit").render(
        {"prompt": "change the sky", "image": "input.png", "seed": 7, "steps": 8}
    )

    assert graph["8"]["inputs"]["text"] == "change the sky"
    assert graph["4"]["inputs"]["image"] == "input.png"
    assert graph["13"]["inputs"]["noise_seed"] == 7
    assert graph["15"]["inputs"]["steps"] == 8
    assert graph["10"]["class_type"] == "ReferenceLatent"
    assert graph["11"]["class_type"] == "ReferenceLatent"


def test_catalog_maps_image_to_video_first_frame():
    catalog = Catalog.load((ROOT / "catalog",))
    graph = catalog.get("minimax-h3-i2v").render(
        {
            "prompt": "walk forward",
            "image": "frame.png",
            "width": 1344,
            "height": 768,
            "length": 124,
            "seed": 3,
            "steps": 16,
        }
    )

    assert graph["5"]["class_type"] == "LoadImage"
    assert graph["5"]["inputs"]["image"] == "frame.png"
    assert graph["6"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert graph["6"]["inputs"]["first_frame"] == ["5", 0]
    assert graph["6"]["inputs"]["prompt"] == "walk forward"
    assert graph["10"]["inputs"]["noise_seed"] == 3
    assert graph["8"]["inputs"]["steps"] == 16


def test_catalog_renders_klein_9b_base_workflow():
    catalog = Catalog.load((ROOT / "catalog",))
    graph = catalog.get("flux-2-klein-base-9b").render(
        {"prompt": "a slow detailed render", "width": 768, "height": 512, "seed": 9}
    )

    assert graph["1"]["inputs"]["unet_name"] == "flux-2-klein-base-9b-fp8.safetensors"
    assert graph["10"]["inputs"]["cfg"] == 5.0
    assert graph["9"]["inputs"]["steps"] == 20
    assert graph["9"]["inputs"]["width"] == 768
    assert graph["9"]["inputs"]["height"] == 512
    assert graph["7"]["inputs"]["noise_seed"] == 9


def test_catalog_exposes_alias_only_once():
    catalog = Catalog.load((ROOT / "catalog",))
    assert [model.id for model in catalog.list()] == [
        "example/checkpoint-text-to-image",
        "flux-2-klein-4b/image-edit",
        "flux-2-klein-4b/text-to-image",
        "flux-2-klein-base-9b/text-to-image",
        "flux-2-klein-9b/image-edit",
        "flux-2-klein-9b/text-to-image",
        "krea-2-turbo/text-to-image",
        "minimax-h3/image-to-video",
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


def test_catalog_hides_workflows_with_unregistered_nodes(tmp_path):
    catalog = Catalog.load((ROOT / "catalog",))
    model = catalog.get("example")
    model.required_files = []

    object_info = {
        "CLIPTextEncode": {},
        "EmptyLatentImage": {},
        "KSampler": {},
        "SaveImage": {},
        "VAEDecode": {},
        "CheckpointLoaderSimple": {},
    }

    assert model.missing_nodes(object_info) == []
    assert catalog.list_available(tmp_path, object_info) == [model]

    assert model.missing_nodes({}) == [
        "CLIPTextEncode",
        "CheckpointLoaderSimple",
        "EmptyLatentImage",
        "KSampler",
        "SaveImage",
        "VAEDecode",
    ]
    assert catalog.list_available(tmp_path, {}) == []


def test_catalog_requires_models_and_nodes_together(tmp_path):
    catalog = Catalog.load((ROOT / "catalog",))
    model = catalog.get("example")
    model.required_files = ["checkpoints/example.safetensors"]

    checkpoint = tmp_path / "checkpoints/example.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"model")

    assert (
        catalog.get_available(
            "example",
            tmp_path,
            {
                "CLIPTextEncode": {},
                "EmptyLatentImage": {},
                "KSampler": {},
                "SaveImage": {},
                "VAEDecode": {},
                "CheckpointLoaderSimple": {},
            },
        ).id
        == "example/checkpoint-text-to-image"
    )
    try:
        catalog.get_available("example", tmp_path, {})
    except KeyError as exc:
        assert "unregistered nodes" in str(exc)
    else:
        raise AssertionError("expected KeyError for unregistered nodes")
