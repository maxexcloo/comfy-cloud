import json

import pytest

from comfy_control.model_pack import pack_file, unpack_file


def test_pack_round_trip(tmp_path):
    source = tmp_path / "model.bin"
    source.write_bytes(b"abcdefghij")
    manifest = pack_file(source, tmp_path / "pack", chunk_size=3)

    assert len(manifest["chunks"]) == 4
    output = unpack_file(tmp_path / "pack" / "model.bin.pack.json", tmp_path / "out")
    assert output.read_bytes() == source.read_bytes()


def test_unpack_rejects_paths_outside_destination(tmp_path):
    manifest = tmp_path / "model.pack.json"
    manifest.write_text(json.dumps({"name": "../model.bin", "chunks": []}))

    with pytest.raises(ValueError, match="model name"):
        unpack_file(manifest, tmp_path / "out")
