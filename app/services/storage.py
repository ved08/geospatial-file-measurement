from pathlib import Path

from fastapi import UploadFile

from app.core.config import settings


async def save_upload(file_id: str, upload: UploadFile) -> Path:
    """Persist the uploaded file under uploads/<file_id>/<filename>."""
    target_dir = settings.upload_dir / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / Path(upload.filename or "upload").name
    with target.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            out.write(chunk)
    return target
