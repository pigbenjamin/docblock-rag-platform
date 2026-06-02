import os
import sys

os.environ.setdefault("LITELLM_PROXY_URL", "http://litellm-proxy:4000")
os.environ.setdefault("LITELLM_API_KEY", "sk-litellm-internal")
os.environ.setdefault("HF_HUB_OFFLINE", "0")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "0")

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
_core_lib  = os.path.join(_repo_root, "libs/docblock-core")
_svc_root  = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

for _p in [_repo_root, _core_lib, _svc_root]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
