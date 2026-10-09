"""Upload ingestion: save the uploaded file to disk and validate it.

Everything here is blocking (disk writes, XML parsing, zip extraction), so the
route runs it in a worker thread to keep the event loop free.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.services.storage import save_upload
from app.services.validation import FileType, validate_upload


@dataclass(frozen=True)
class StoredUpload:
    filename: str  # sanitised name the file was saved under
    file_type: FileType
    dataset_path: Path  # the .kml, or the extracted .shp, to read features from


def store_upload(
    source: BinaryIO, filename: str | None, file_type: FileType, upload_dir: Path
) -> StoredUpload:
    try:
        saved_path = save_upload(source, filename, upload_dir)
        dataset_path = validate_upload(saved_path, file_type, upload_dir / "extracted")
    except BaseException:
        # Rejected or failed uploads must not leave files behind.
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    return StoredUpload(filename=saved_path.name, file_type=file_type, dataset_path=dataset_path)
