from pathlib import Path

from catalogue.validation import validate_repository

ROOT = Path(__file__).parents[2]


def test_bundled_catalogue_matches_profiles():
    validate_repository(ROOT / "catalogue")
