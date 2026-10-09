import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.files import FileInfo, FileStatus, MeasurementsResponse
from app.services.repository import repository
from app.services.uploads import store_upload
from app.services.validation import UploadValidationError, detect_file_type

router = APIRouter(prefix="/files", tags=["files"])


def _get_file_or_404(file_id: str) -> FileInfo:
    file = repository.get(file_id)
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return file


@router.post("/", response_model=FileInfo, status_code=status.HTTP_201_CREATED)
async def upload_file(file: UploadFile) -> FileInfo:
    """Upload a .zip (Shapefile) or .kml file for processing."""
    file_id = uuid.uuid4().hex
    try:
        file_type = detect_file_type(file.filename)
        # Saving and validating block (disk writes, XML parsing, zip extraction);
        # run them in a worker thread so other requests are served meanwhile.
        stored = await run_in_threadpool(
            store_upload, file.file, file.filename, file_type, settings.upload_dir / file_id
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None

    record = FileInfo(
        id=file_id,
        filename=stored.filename,
        status=FileStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    repository.add(record)
    # TODO: read features from stored.dataset_path, compute measurements, mark COMPLETED/FAILED.
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
