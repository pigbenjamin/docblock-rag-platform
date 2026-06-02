# core/marker_runner.py
from __future__ import annotations

import logging
import subprocess
import shutil
from pathlib import Path
from typing import Optional

from .logging_utils import get_file_logger


def _pick_md_from_out_dir(out_dir: Path) -> Path:
    """
    Try best effort to find a markdown file produced by marker under out_dir.
    Strategy:
      1) If exactly one *.md exists (recursive), use it.
      2) Prefer largest *.md (common when there are multiple small files)
    """
    mds = list(out_dir.rglob("*.md"))
    if not mds:
        raise FileNotFoundError(f"No .md found under marker out_dir: {out_dir}")

    if len(mds) == 1:
        return mds[0]

    # choose the largest md
    mds.sort(key=lambda p: p.stat().st_size, reverse=True)
    return mds[0]


def run_marker(
    *,
    job_id: str,
    doc_id: str,
    pdf_path: str,
    out_dir: str,
    marker_cmd: str,
    timeout: int = 1800,
    logger: Optional[logging.Logger] = None,
) -> str:
    """
    External tool boundary (Marker).

    Supports TWO marker_cmd styles:

    A) File-output style (if your marker supports it):
       marker_cmd contains {pdf} and {md}
       Example: marker --input "{pdf}" --output "{md}"

    B) Directory-output style (common):
       marker_cmd contains {pdf} and {out_dir}
       Example: marker_single "{pdf}" --output_dir "{out_dir}"
       In this case we will locate the produced *.md under out_dir and copy it to out_md.
    """
    pdf_path = str(Path(pdf_path).resolve())
    #out_path = Path(out_md).resolve()
    out_path = Path(out_dir).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    print(f"Running marker for PDF: {pdf_path}, output dir: {out_path}")

    logger = logger or logging.getLogger("core.marker")

    # Decide mode by placeholders
    has_pdf = "{pdf}" in marker_cmd
    has_md = "{md}" in marker_cmd
    has_out_dir = "{out_dir}" in marker_cmd

    if not has_pdf:
        raise ValueError('marker_cmd must contain "{pdf}" placeholder')

    # Mode B: output_dir
    if has_out_dir and not has_md:
        #out_dir = out_path.parent / "_marker_out"
        out_dir = out_path
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = marker_cmd.format(pdf=pdf_path, out_dir=str(out_dir))
        logger.info("[marker] job_id=%s doc_id=%s run (dir mode): %s", job_id, doc_id, cmd)
        logger.info("[marker] job_id=%s doc_id=%s pdf_path=%s out_dir=%s out_md=%s", job_id, doc_id, pdf_path, out_dir, out_path)

        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        #logger.info("[marker] job_id=%s doc_id=%s stdout:\n%s", job_id, doc_id, proc.stdout or "")
        #logger.info("[marker] job_id=%s doc_id=%s stderr:\n%s", job_id, doc_id, proc.stderr or "")
        logger.info("[marker] job_id=%s doc_id=%s returncode=%s", job_id, doc_id, proc.returncode)

        if proc.returncode != 0:
            logger.error("[marker] job_id=%s doc_id=%s marker failed with return code %s. See log for details.", job_id, doc_id, proc.returncode)
            raise RuntimeError(f"marker failed rc={proc.returncode}. See log for details.")

        # Rename marker's output dir (named after pdf stem) to doc_id
        marker_out_dir = out_path / Path(pdf_path).stem
        raw_md_dir = out_path / doc_id
        if marker_out_dir != raw_md_dir:
            # Different paths: remove stale raw_md_dir first, then rename
            if raw_md_dir.exists():
                shutil.rmtree(raw_md_dir)
            if not marker_out_dir.exists():
                raise FileNotFoundError(f"Marker finished but expected output dir not found: {marker_out_dir}")
            marker_out_dir.rename(raw_md_dir)
        logger.info("[marker] job_id=%s doc_id=%s marker output dir: %s", job_id, doc_id, raw_md_dir)
        #print(f"Renamed marker output dir from {out_path / (Path(pdf_path).stem)} to: {raw_md_dir}")        
        
        produced_md = _pick_md_from_out_dir(raw_md_dir)
        # copy produced_md to raw_md_path (which is out_path/pdf_stem/raw.md)
        raw_md_path = raw_md_dir / "raw.md"
        logger.info("[marker] job_id=%s doc_id=%s Copied produced MD %s to %s", job_id, doc_id, produced_md, raw_md_path)
        #print(f"Copying produced MD to: {raw_md_path}")
        shutil.copyfile(produced_md, raw_md_path)
        
        logger.info("[marker] job_id=%s doc_id=%s picked md: %s -> %s", job_id, doc_id, produced_md, raw_md_path)

        return str(raw_md_path)

    # Mode A: output file
    if has_md and not has_out_dir:
        cmd = marker_cmd.format(pdf=pdf_path, md=str(out_path))
        logger.info("[marker] job_id=%s doc_id=%s run (file mode): %s", job_id, doc_id, cmd)
        logger.info("[marker] job_id=%s doc_id=%s pdf_path=%s out_md=%s", job_id, doc_id, pdf_path, out_path)

        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        #logger.info("[marker] job_id=%s doc_id=%s stdout:\n%s", job_id, doc_id, proc.stdout or "")
        #logger.info("[marker] job_id=%s doc_id=%s stderr:\n%s", job_id, doc_id, proc.stderr or "")
        logger.info("[marker] job_id=%s doc_id=%s returncode=%s", job_id, doc_id, proc.returncode)

        if proc.returncode != 0:
            logger.error("[marker] job_id=%s doc_id=%s marker failed with return code %s. See log for details.", job_id, doc_id, proc.returncode)
            raise RuntimeError(f"marker failed rc={proc.returncode}. See log for details.")

        if not out_path.exists():
            logger.error("[marker] job_id=%s doc_id=%s Marker finished but output md not found: %s", job_id, doc_id, out_path)
            raise FileNotFoundError(f"Marker finished but output md not found: {out_path}")

        return str(out_path)

    # If both placeholders exist, that's ambiguous—force you to choose one style
    if has_md and has_out_dir:
        logger.error("[marker] job_id=%s doc_id=%s marker_cmd should use either '{md}' OR '{out_dir}', not both", job_id, doc_id)
        raise ValueError('marker_cmd should use either "{md}" OR "{out_dir}", not both')
    logger.error("[marker] job_id=%s doc_id=%s marker_cmd must contain either '{md}' (file mode) or '{out_dir}' (dir mode)", job_id, doc_id)
    raise ValueError('marker_cmd must contain either "{md}" (file mode) or "{out_dir}" (dir mode)')
