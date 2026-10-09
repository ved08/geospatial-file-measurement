from pathlib import Path
from typing import BinaryIO

from app.core.config import settings
from app.services.validation import UploadTooLargeError

CHUNK_SIZE = 1024 * 1024


def safe_filename(filename: str | None) -> str:
    """Drop any directory part a client may send, e.g. '../../evil.kml' -> 'evil.kml'."""
    name = Path(filename or "").name
    return name if name not in ("", ".", "..") else "upload"


def save_upload(source: BinaryIO, filename: str | None, target_dir: Path) -> Path:
    """Copy the upload to target_dir in chunks, aborting once it exceeds the size limit."""
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_filename(filename)

    source.seek(0)
    written = 0
    with target.open("wb") as out:
        while chunk := source.read(CHUNK_SIZE):
            written += len(chunk)
            if written > max_bytes:
                raise UploadTooLargeError()
            out.write(chunk)
    return target
