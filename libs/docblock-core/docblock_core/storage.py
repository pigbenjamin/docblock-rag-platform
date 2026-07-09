# core/storage.py
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


class LocalFileStorage:
    """Filesystem-backed storage for uploaded documents, shared by
    document-api and ingest-worker over the same mounted volume.

    A document's final version (`{tenant}/{document_id}/v{n}/`) isn't known
    at upload time - `ensure_document_version` only resolves it once ingest
    runs. So uploads land in a job-scoped temp directory first via
    `save_temp`, and `finalize` moves the file into its permanent location
    once the version is known.
    """

    def __init__(self, base_dir: PathLike):
        self.base_dir = Path(base_dir)

    def save_temp(self, job_id: str, filename: str, content: bytes) -> Path:
        dest_dir = self.base_dir / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename
        dest_path.write_bytes(content)
        return dest_path

    def finalize(
        self,
        *,
        tenant_id: str,
        document_id: str,
        version: int,
        temp_path: PathLike,
    ) -> Path:
        temp_path = Path(temp_path)
        final_dir = self.base_dir / tenant_id / document_id / f"v{version}"
        final_dir.mkdir(parents=True, exist_ok=True)
        final_path = final_dir / temp_path.name

        shutil.move(str(temp_path), str(final_path))

        try:
            temp_path.parent.rmdir()
        except OSError:
            pass  # not empty (other artifacts still in the job dir) - leave it

        return final_path
