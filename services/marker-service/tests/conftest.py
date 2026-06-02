import os
import sys

# Set env vars before any docblock_core import so config loads cleanly
os.environ.setdefault("MARKER_CMD", 'marker_single "{pdf}" --output_dir "{out_dir}"')
os.environ.setdefault("MARKER_TIMEOUT", "1800")
os.environ.setdefault("HF_HUB_OFFLINE", "0")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "0")

import pytest
from fastapi.testclient import TestClient

# Allow imports from services/marker-service and repo root
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
_core_lib  = os.path.join(_repo_root, "libs/docblock-core")
_svc_root  = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

for _p in [_repo_root, _core_lib, _svc_root]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c
