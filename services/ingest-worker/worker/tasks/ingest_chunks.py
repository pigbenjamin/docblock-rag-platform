from __future__ import annotations

from docblock_core.ingest import ingest


def run_ingest_chunks(chunk_block_json: str) -> None:
    """Ingest a chunk_block.json file into PostgreSQL."""
    ingest(chunk_block_json)
