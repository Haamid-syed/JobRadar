import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def make_config():
    """Return the smallest application-shaped config needed by these tests."""
    return SimpleNamespace(
        profile=SimpleNamespace(
            experience_years=1,
            skills=SimpleNamespace(
                primary=["Python", "React.js"],
                secondary=["FastAPI"],
                niche=["WebRTC"],
            ),
        ),
        filters=SimpleNamespace(
            max_age_days=14,
            exclude_keywords=[],
            exclude_company_keywords=[],
            role_types=["software engineer", "backend", "frontend"],
        ),
    )


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
