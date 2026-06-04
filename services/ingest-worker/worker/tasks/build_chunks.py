from __future__ import annotations

from typing import Optional

from docblock_core.chunk_builder import build_blocks
from docblock_core.config import settings


def run_build_chunks(
    fixed_md: str,
    out_json: str,
    doc_id: Optional[str] = None,
    source_path: Optional[str] = None,
    tenant_id: Optional[str] = None,
    document_id: Optional[str] = None,
) -> str:
    """Build chunk_block.json from a fixed Markdown file. Returns the output JSON path."""
    return build_blocks(
        fixed_md=fixed_md,
        out_json=out_json,
        doc_id=doc_id or "",
        source_path=source_path or "",
        tenant_id=tenant_id or settings.db.tenant_id,
        document_id=document_id,
        seg_model=settings.models.seg_model,
        ollama_gen_url=settings.models.litellm_base_url,
        infer_table_capabilities=settings.chunking.infer_table_capabilities,
        summarize_tables=settings.chunking.summarize_tables,
        capabilities_model=settings.chunking.capabilities_model,
    )
