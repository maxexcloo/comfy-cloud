from comfy_cloud.model_pack import pack_file, unpack_file


def test_pack_round_trip(tmp_path):
    source = tmp_path / "model.bin"
    source.write_bytes(b"abcdefghij")
    manifest = pack_file(source, tmp_path / "pack", chunk_size=3)

    assert len(manifest["chunks"]) == 4
    output = unpack_file(tmp_path / "pack" / "model.bin.pack.json", tmp_path / "out")
    assert output.read_bytes() == source.read_bytes()
