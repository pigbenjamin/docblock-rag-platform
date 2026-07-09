"""
Tests for worker/tasks/marker_to_md.py.

Mocks httpx.Client so no real LiteLLM connection is needed.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

FAKE_MD_PATH = "/data/out/doc1/raw.md"


def _make_mock_client(md_path: str = FAKE_MD_PATH):
    """Return a mock httpx client context manager that responds with md_path."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": md_path}}]
    }
    mock_resp.raise_for_status.return_value = None

    mock_instance = MagicMock()
    mock_instance.post.return_value = mock_resp

    mock_cls = MagicMock()
    mock_cls.return_value.__enter__.return_value = mock_instance
    mock_cls.return_value.__exit__.return_value = False

    return mock_cls, mock_instance


# ── happy path ───────────────────────────────────────────────────

def test_run_marker_to_md_returns_md_path():
    mock_cls, _ = _make_mock_client(FAKE_MD_PATH)
    with patch("worker.tasks.marker_to_md.httpx.Client", mock_cls):
        from worker.tasks.marker_to_md import run_marker_to_md
        result = run_marker_to_md("job1", "doc1", "/data/test.pdf", "/data/out")

    assert result == FAKE_MD_PATH


def test_run_marker_to_md_posts_to_litellm_proxy():
    mock_cls, mock_instance = _make_mock_client()
    with patch("worker.tasks.marker_to_md.httpx.Client", mock_cls):
        from worker.tasks.marker_to_md import run_marker_to_md
        run_marker_to_md("job1", "doc1", "/data/test.pdf", "/data/out")

    mock_instance.post.assert_called_once()
    call_args = mock_instance.post.call_args

    url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
    assert "/v1/chat/completions" in url


def test_run_marker_to_md_payload_structure():
    mock_cls, mock_instance = _make_mock_client()
    with patch("worker.tasks.marker_to_md.httpx.Client", mock_cls):
        from worker.tasks.marker_to_md import run_marker_to_md
        run_marker_to_md("job-99", "my-doc", "/data/x.pdf", "/data/work")

    sent_json = mock_instance.post.call_args.kwargs["json"]

    assert sent_json["model"] == "marker/pdf-to-md"
    assert len(sent_json["messages"]) == 1

    content = json.loads(sent_json["messages"][0]["content"])
    assert content["pdf_path"] == "/data/x.pdf"
    assert content["document_id"] == "my-doc"
    assert content["out_dir"] == "/data/work"
    assert content["job_id"] == "job-99"


def test_run_marker_to_md_sends_auth_header():
    mock_cls, mock_instance = _make_mock_client()
    with patch("worker.tasks.marker_to_md.httpx.Client", mock_cls):
        from worker.tasks.marker_to_md import run_marker_to_md
        run_marker_to_md("job1", "doc1", "/data/test.pdf", "/data/out")

    headers = mock_instance.post.call_args.kwargs.get("headers", {})
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")


# ── error handling ───────────────────────────────────────────────

def test_run_marker_to_md_raises_on_http_error():
    import httpx

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error",
        request=MagicMock(),
        response=MagicMock(status_code=500),
    )

    mock_instance = MagicMock()
    mock_instance.post.return_value = mock_resp

    mock_cls = MagicMock()
    mock_cls.return_value.__enter__.return_value = mock_instance
    mock_cls.return_value.__exit__.return_value = False

    with patch("worker.tasks.marker_to_md.httpx.Client", mock_cls):
        from worker.tasks.marker_to_md import run_marker_to_md
        with pytest.raises(httpx.HTTPStatusError):
            run_marker_to_md("job1", "doc1", "/data/test.pdf", "/data/out")


def test_run_marker_to_md_raises_on_missing_choices():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}  # no "choices" key
    mock_resp.raise_for_status.return_value = None

    mock_instance = MagicMock()
    mock_instance.post.return_value = mock_resp

    mock_cls = MagicMock()
    mock_cls.return_value.__enter__.return_value = mock_instance
    mock_cls.return_value.__exit__.return_value = False

    with patch("worker.tasks.marker_to_md.httpx.Client", mock_cls):
        from worker.tasks.marker_to_md import run_marker_to_md
        with pytest.raises((KeyError, TypeError)):
            run_marker_to_md("job1", "doc1", "/data/test.pdf", "/data/out")
