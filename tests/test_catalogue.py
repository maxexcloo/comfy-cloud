from pathlib import Path

import pytest

from comfy_control.catalogue import Catalogue, WorkflowModel

ROOT = Path(__file__).parents[1]


def test_catalogue_loads_and_renders_workflow():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("example")
    graph = model.render({"prompt": "a red fox", "width": 768, "seed": 42})

    assert model.id == "example/checkpoint-text-to-image"
    assert graph["1"]["inputs"]["text"] == "a red fox"
    assert graph["2"]["inputs"]["width"] == 768
    assert graph["3"]["inputs"]["seed"] == 42


def test_catalogue_maps_one_parameter_to_multiple_nodes():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("flux-2-klein-4b").render({"height": 768, "width": 1344})

    assert graph["6"]["inputs"]["height"] == 768
    assert graph["6"]["inputs"]["width"] == 1344
    assert graph["9"]["inputs"]["height"] == 768
    assert graph["9"]["inputs"]["width"] == 1344


def test_catalogue_maps_image_edit_inputs_to_reference_workflow():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("flux-2-klein-4b-edit").render(
        {"prompt": "change the sky", "image": "input.png", "seed": 7, "steps": 8}
    )

    assert graph["8"]["inputs"]["text"] == "change the sky"
    assert graph["4"]["inputs"]["image"] == "input.png"
    assert graph["13"]["inputs"]["noise_seed"] == 7
    assert graph["15"]["inputs"]["steps"] == 8
    assert graph["10"]["class_type"] == "ReferenceLatent"
    assert graph["11"]["class_type"] == "ReferenceLatent"


@pytest.mark.parametrize(
    ("references", "nodes"),
    [
        (2, ("4", "20")),
        (3, ("4", "20", "25")),
        (4, ("4", "20", "25", "30")),
    ],
)
def test_catalogue_maps_every_ordered_reference_to_its_load_node(references, nodes):
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get(f"flux-2-klein-9b/image-edit-{references}-reference")
    values = {
        f"image_{index}": f"upload-{index}.png" for index in range(1, references + 1)
    }
    graph = model.render(values)

    assert model.reference_input_names == tuple(values)
    assert model.reference_image_count == references
    for index, node in enumerate(nodes, start=1):
        assert graph[node]["class_type"] == "LoadImage"
        assert graph[node]["inputs"]["image"] == f"upload-{index}.png"


def image_edit_manifest(input_names):
    return {
        "id": "test/image-edit",
        "profile": "test",
        "operation": "image_edit",
        "workflow": "workflow.json",
        "input_map": {
            "prompt": {"node": "1", "input": "text"},
            **{
                name: {"node": str(index + 2), "input": "image"}
                for index, name in enumerate(input_names)
            },
        },
        "output": {"node": "9", "type": "image"},
    }


def test_catalogue_rejects_non_contiguous_numbered_image_inputs():
    with pytest.raises(ValueError, match="begin at image_1 and contain no gaps"):
        WorkflowModel.model_validate(image_edit_manifest(("image_1", "image_3")))


def test_catalogue_rejects_legacy_and_numbered_image_inputs_together():
    with pytest.raises(ValueError, match="must not mix image"):
        WorkflowModel.model_validate(image_edit_manifest(("image", "image_1")))


def test_catalogue_maps_image_to_video_first_frame():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("minimax-h3-i2v").render(
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


def test_catalogue_renders_klein_9b_base_workflow():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    graph = catalogue.get("flux-2-klein-base-9b").render(
        {"prompt": "a slow detailed render", "width": 768, "height": 512, "seed": 9}
    )

    assert graph["1"]["inputs"]["unet_name"] == "flux-2-klein-base-9b-fp8.safetensors"
    assert graph["10"]["inputs"]["cfg"] == 5.0
    assert graph["9"]["inputs"]["steps"] == 20
    assert graph["9"]["inputs"]["width"] == 768
    assert graph["9"]["inputs"]["height"] == 512
    assert graph["7"]["inputs"]["noise_seed"] == 9


def test_catalogue_exposes_alias_only_once():
    catalogue = Catalogue.load((ROOT / "catalogue",))
    assert [model.id for model in catalogue.list()] == [
        "example/checkpoint-text-to-image",
        "flux-2-klein-4b/image-edit",
        "flux-2-klein-4b/text-to-image",
        "flux-2-klein-base-9b/text-to-image",
        "flux-2-klein-9b/image-edit",
        "flux-2-klein-9b/image-edit-2-reference",
        "flux-2-klein-9b/image-edit-3-reference",
        "flux-2-klein-9b/image-edit-4-reference",
        "flux-2-klein-9b/text-to-image",
        "krea-2-turbo/text-to-image",
        "minimax-h3/image-to-video",
        "minimax-h3/text-to-video",
    ]


def test_catalogue_only_exposes_workflows_with_required_models(tmp_path):
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("example")
    model.required_files = ["checkpoints/example.safetensors"]

    assert model.missing_files(tmp_path) == ["checkpoints/example.safetensors"]
    assert catalogue.list_available(tmp_path) == []

    checkpoint = tmp_path / "checkpoints/example.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"model")

    assert catalogue.list_available(tmp_path) == [model]


def test_catalogue_hides_workflows_with_unregistered_nodes(tmp_path):
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("example")
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
    assert catalogue.list_available(tmp_path, object_info) == [model]

    assert model.missing_nodes({}) == [
        "CLIPTextEncode",
        "CheckpointLoaderSimple",
        "EmptyLatentImage",
        "KSampler",
        "SaveImage",
        "VAEDecode",
    ]
    assert catalogue.list_available(tmp_path, {}) == []


def test_catalogue_requires_models_and_nodes_together(tmp_path):
    catalogue = Catalogue.load((ROOT / "catalogue",))
    model = catalogue.get("example")
    model.required_files = ["checkpoints/example.safetensors"]

    checkpoint = tmp_path / "checkpoints/example.safetensors"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"model")

    assert (
        catalogue.get_available(
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
        catalogue.get_available("example", tmp_path, {})
    except KeyError as exc:
        assert "unregistered nodes" in str(exc)
    else:
        raise AssertionError("expected KeyError for unregistered nodes")
