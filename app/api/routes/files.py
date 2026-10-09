import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, status

from app.core.config import settings
from app.schemas.files import FileInfo, FileStatus, MeasurementsResponse
from app.services.repository import repository
from app.services.storage import save_upload

router = APIRouter(prefix="/files", tags=["files"])


def _get_file_or_404(file_id: str) -> FileInfo:
    file = repository.get(file_id)
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return file


@router.post("/", response_model=FileInfo, status_code=status.HTTP_201_CREATED)
async def upload_file(file: UploadFile) -> FileInfo:
    """Upload a .zip (Shapefile) or .kml file for processing."""
    extension = Path(file.filename or "").suffix.lower()
    if extension not in settings.allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{extension}'. Allowed: {', '.join(settings.allowed_extensions)}",
        )

    file_id = uuid.uuid4().hex
    await save_upload(file_id, file)

    record = FileInfo(
        id=file_id,
        filename=file.filename,
        status=FileStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    repository.add(record)
    # TODO: parse features and compute measurements, then mark COMPLETED/FAILED.
    return record


@router.get("/{file_id}/", response_model=FileInfo)
def get_file(file_id: str) -> FileInfo:
    """Return metadata and processing status for an uploaded file."""
    return _get_file_or_404(file_id)


@router.get("/{file_id}/measurements/", response_model=MeasurementsResponse)
def get_measurements(file_id: str) -> MeasurementsResponse:
    """Return per-feature measurements for an uploaded file."""
    _get_file_or_404(file_id)
    features = repository.get_measurements(file_id)
    return MeasurementsResponse(file_id=file_id, feature_count=len(features), features=features)
