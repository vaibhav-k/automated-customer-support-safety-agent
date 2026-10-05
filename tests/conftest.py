"""Shared pytest setup: make the repo root and the Function App (src/) importable."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (REPO_ROOT, REPO_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
