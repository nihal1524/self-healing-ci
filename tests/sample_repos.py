from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def requires_sample(name: str):
    """Skip a test when a sample repo is missing: the sample-* folders are gitignored (each is its own
    git repo), so they exist locally but not in a fresh clone such as GitHub CI."""
    return pytest.mark.skipif(
        not (PROJECT_ROOT / name).is_dir(),
        reason=f"{name} is gitignored and not present in a fresh clone",
    )
