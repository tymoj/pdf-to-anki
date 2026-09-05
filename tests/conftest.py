from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def sample_pdf() -> Path:
    path = REPO_ROOT / "pdf" / "1.praktikum hematopoees.pdf"
    if not path.exists():
        pytest.skip(f"sample PDF missing: {path}")
    return path
