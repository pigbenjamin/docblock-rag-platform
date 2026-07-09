from __future__ import annotations

from typing import Tuple

from docblock_core.ingest import ingest


def run_ingest_chunks(chunk_block_json: str) -> Tuple[str, int]:
    """Ingest a chunk_block.json file into PostgreSQL. Returns (document_id, active_version)."""
    return ingest(chunk_block_json)
