from pathlib import Path

from comfy_cloud.validation import validate_repository

ROOT = Path(__file__).parents[1]


def test_bundled_catalogue_matches_profiles():
    validate_repository(ROOT / "catalogue", ROOT / "profiles")
