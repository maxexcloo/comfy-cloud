from comfy_control.model_profiles import profile_details, required_vram_gb


def test_model_profile_details_and_combined_vram_requirement():
    details = profile_details("flux-2-klein-9b")

    assert details["minimum_vram_gb"] == 24
    assert "diffusion_models/flux-2-klein-9b-fp8.safetensors" in details["assets"]
    assert required_vram_gb(["flux-2-klein-9b", "krea-2-turbo"]) == 48
