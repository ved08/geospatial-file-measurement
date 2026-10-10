import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.files import FileInfo, FileStatus, MeasurementsResponse
from app.services.pipeline import process_file
from app.services.repository import FileRepository, get_repository
from app.services.uploads import store_upload
from app.services.validation import UploadValidationError, detect_file_type

router = APIRouter(prefix="/files", tags=["files"])

MAX_PAGE_SIZE = 10_000

# Injected rather than imported, so tests can swap it (app.dependency_overrides).
Repository = Annotated[FileRepository, Depends(get_repository)]

NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"description": "No file with this id"}}


def _get_file_or_404(repository: FileRepository, file_id: str) -> FileInfo:
    file = repository.get(file_id)
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return file


@router.post(
    "/",
    response_model=FileInfo,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Unsupported or invalid file"},
        413: {"description": "File too large"},
    },
)
async def upload_file(
    file: UploadFile, background_tasks: BackgroundTasks, repository: Repository
) -> FileInfo:
    """Upload a .zip (Shapefile) or .kml file for processing.

    The file is validated immediately and the response (status PENDING) is sent
    as soon as it is stored. Reading and measuring the features happens in the
    background: poll `GET /api/files/{id}/` until the status is COMPLETED or
    FAILED, then fetch `GET /api/files/{id}/measurements/`.
    """
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
    # A short blocking insert; keep it off the event loop like the rest.
    await run_in_threadpool(repository.add, record)
    # Runs after the response has been sent. process_file is a plain (sync)
    # function, so Starlette's BackgroundTask runs it with run_in_threadpool:
    # it occupies a worker thread, never the event loop.
    background_tasks.add_task(process_file, file_id, stored.dataset_path, repository)
    return record


@router.get("/{file_id}/", response_model=FileInfo, responses=NOT_FOUND)
def get_file(file_id: str, repository: Repository) -> FileInfo:
    """Return metadata and processing status for an uploaded file.

    `feature_count` and `crs` are filled in once the status is COMPLETED; if it
    is FAILED, `error` says why.
    """
    return _get_file_or_404(repository, file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    responses={
        **NOT_FOUND,
        status.HTTP_409_CONFLICT: {"description": "File is still processing, or processing failed"},
    },
)
def get_measurements(
    file_id: str,
    repository: Repository,
    offset: int = Query(0, ge=0, description="Number of features to skip"),
    limit: int | None = Query(
        None, ge=1, le=MAX_PAGE_SIZE, description="Maximum number of features to return (default: all)"
    ),
) -> MeasurementsResponse:
    """Return per-feature measurements for a processed file.

    Polygons get an area and perimeter, lines a length; points and other
    geometries without size get a `note` instead. `total` is the number of
    features in the file; use `offset`/`limit` to page through them.
    """
    file = _get_file_or_404(repository, file_id)
    if file.status is FileStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"File processing failed: {file.error}"
        )
    if file.status is not FileStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"File is still being processed (status: {file.status.value})",
        )

    return MeasurementsResponse(
        file_id=file_id,
        crs=file.crs,
        total=repository.count_measurements(file_id),
        offset=offset,
        limit=limit,
        summary=repository.get_summary(file_id),
        features=repository.get_measurements(file_id, offset, limit),
    )
