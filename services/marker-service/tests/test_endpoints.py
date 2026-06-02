"""
Tests for marker-service FastAPI endpoints.

All tests mock `run_marker` so no actual marker CLI or GPU is required.
"""
from __future__ import annotations

import json
from unittest.mock import patch

FAKE_MD_PATH = "/data/out/doc1/raw.md"

CONVERT_PAYLOAD = {
    "pdf_path": "/data/test.pdf",
    "doc_id": "doc1",
    "out_dir": "/data/out",
}

CHAT_PAYLOAD = {
    "model": "marker/pdf-to-md",
    "messages": [{
        "role": "user",
        "content": json.dumps({
            "pdf_path": "/data/test.pdf",
            "doc_id": "doc1",
            "out_dir": "/data/out",
            "job_id": "test-job-1",
        }),
    }],
}


# ── /healthz ─────────────────────────────────────────────────────

def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── /v1/convert ──────────────────────────────────────────────────

def test_convert_success(client):
    with patch("app.main.run_marker", return_value=FAKE_MD_PATH):
        resp = client.post("/v1/convert", json=CONVERT_PAYLOAD)

    assert resp.status_code == 200
    data = resp.json()
    assert data["md_path"] == FAKE_MD_PATH
    assert data["doc_id"] == "doc1"
    assert data["elapsed"] >= 0


def test_convert_passes_correct_args(client):
    with patch("app.main.run_marker", return_value=FAKE_MD_PATH) as mock_run:
        client.post("/v1/convert", json={**CONVERT_PAYLOAD, "job_id": "j42"})

    call_kw = mock_run.call_args.kwargs
    assert call_kw["pdf_path"] == "/data/test.pdf"
    assert call_kw["doc_id"] == "doc1"
    assert call_kw["out_dir"] == "/data/out"
    assert call_kw["job_id"] == "j42"


def test_convert_marker_failure_returns_500(client):
    with patch("app.main.run_marker", side_effect=RuntimeError("marker failed rc=1")):
        resp = client.post("/v1/convert", json=CONVERT_PAYLOAD)

    assert resp.status_code == 500
    assert "marker failed" in resp.json()["detail"]


def test_convert_missing_required_field(client):
    resp = client.post("/v1/convert", json={"pdf_path": "/data/test.pdf"})
    assert resp.status_code == 422  # pydantic validation error


# ── /v1/chat/completions (OpenAI-compatible) ─────────────────────

def test_chat_completions_success(client):
    with patch("app.main.run_marker", return_value=FAKE_MD_PATH):
        resp = client.post("/v1/chat/completions", json=CHAT_PAYLOAD)

    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == FAKE_MD_PATH
    assert choice["finish_reason"] == "stop"
    assert "usage" in data


def test_chat_completions_doc_id_defaults_to_pdf_stem(client):
    payload = {
        "model": "marker/pdf-to-md",
        "messages": [{
            "role": "user",
            "content": json.dumps({
                "pdf_path": "/data/report.pdf",
                "out_dir": "/data/out",
                # doc_id intentionally omitted
            }),
        }],
    }
    with patch("app.main.run_marker", return_value=FAKE_MD_PATH) as mock_run:
        resp = client.post("/v1/chat/completions", json=payload)

    assert resp.status_code == 200
    assert mock_run.call_args.kwargs["doc_id"] == "report"


def test_chat_completions_missing_pdf_path_returns_400(client):
    payload = {
        "model": "marker/pdf-to-md",
        "messages": [{
            "role": "user",
            "content": json.dumps({"doc_id": "doc1", "out_dir": "/data/out"}),
        }],
    }
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400


def test_chat_completions_invalid_json_content_returns_400(client):
    payload = {
        "model": "marker/pdf-to-md",
        "messages": [{"role": "user", "content": "not-json-at-all"}],
    }
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 400


def test_chat_completions_empty_messages_returns_400(client):
    resp = client.post("/v1/chat/completions", json={"model": "marker/pdf-to-md", "messages": []})
    assert resp.status_code == 400


def test_chat_completions_marker_failure_returns_500(client):
    with patch("app.main.run_marker", side_effect=FileNotFoundError("/data/test.pdf not found")):
        resp = client.post("/v1/chat/completions", json=CHAT_PAYLOAD)

    assert resp.status_code == 500
