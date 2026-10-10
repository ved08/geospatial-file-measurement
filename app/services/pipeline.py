"""Processing pipeline: read a validated upload, measure it, store the results.

`process_file` is plain synchronous code with no FastAPI dependency. Today the
upload route runs it as a background task; a task queue worker (RQ, Celery...)
could call the same function unchanged: without a `repository` argument it uses
the one configured by `GEO_DATABASE_URL`.

It never raises: whatever happens, the file ends up COMPLETED or FAILED, so a
client polling its status is never left waiting on a file stuck in PROCESSING.
"""

import logging
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import shapely

from app.schemas.files import FeatureMeasurement, FileStatus, MeasurementSummary
from app.services.measurement import measure_feature
from app.services.processing import ParsedFeature, ProcessingError, crs_label, read_dataset
from app.services.repository import FileRepository, get_repository

logger = logging.getLogger(__name__)

# Shown to clients for unexpected errors; the details go to the log only.
INTERNAL_ERROR_MESSAGE = "Internal error while processing the file"
INTERRUPTED_MESSAGE = "Processing was interrupted by a server restart; please upload the file again"


def process_file(file_id: str, dataset_path: Path, repository: FileRepository | None = None) -> None:
    """Read, measure and store every feature of an uploaded file."""
    started = time.perf_counter()
    if repository is None:
        repository = get_repository()
    try:
        repository.update(file_id, status=FileStatus.PROCESSING)
        dataset = read_dataset(dataset_path)
        features = [_measure(feature) for feature in dataset.features]
        # Measurements are committed before the status flips (two transactions,
        # in this order), so anyone who sees COMPLETED is guaranteed to find them.
        repository.set_measurements(file_id, features, _summarise(features))
        repository.update(
            file_id,
            status=FileStatus.COMPLETED,
            feature_count=len(features),
            crs=crs_label(dataset.crs) if dataset.crs is not None else None,
            warnings=dataset.warnings,
            processed_at=_now(),
        )
    except ProcessingError as exc:
        logger.info("File %s failed processing: %s", file_id, exc.message)
        _fail(repository, file_id, exc.message)
    except Exception:
        logger.exception("Unexpected error while processing file %s", file_id)
        _fail(repository, file_id, INTERNAL_ERROR_MESSAGE)
    else:
        logger.info(
            "Processed file %s: %d features in %.2fs",
            file_id,
            len(features),
            time.perf_counter() - started,
        )


def fail_interrupted_files(repository: FileRepository) -> list[str]:
    """Mark files left PENDING/PROCESSING by a previous run as FAILED.

    Run at startup. Background tasks live in the server process, so after a
    crash or restart nothing will ever pick these files up again; without this
    their clients would poll forever. (A durable task queue would retry them
    instead.) Assumes no other process is processing files at the time: with
    several API processes sharing a database, a restarting one would also fail
    files that another is still working on.
    """
    file_ids = repository.fail_interrupted(INTERRUPTED_MESSAGE, processed_at=_now())
    if file_ids:
        logger.warning("Marked %d interrupted file(s) as failed: %s", len(file_ids), ", ".join(file_ids))
    return file_ids


def _fail(repository: FileRepository, file_id: str, message: str) -> None:
    try:
        repository.update(file_id, status=FileStatus.FAILED, error=message, processed_at=_now())
    except Exception:
        # Nothing more can be done (e.g. the record is gone); don't kill the worker.
        logger.exception("Could not mark file %s as failed", file_id)


def _measure(feature: ParsedFeature) -> FeatureMeasurement:
    measurement = measure_feature(feature.geometry, feature.crs)
    geometry, note = feature.geojson, measurement.note
    if feature.geometry is not None and not _has_finite_coordinates(feature.geometry):
        # NaN/Infinity aren't valid JSON, so such a geometry can't be returned.
        geometry = None
        note = "; ".join(filter(None, [note, "Geometry has non-finite coordinates and is omitted"]))
    return FeatureMeasurement(
        id=feature.id,
        layer=feature.layer,
        geometry_type=feature.geometry_type,
        geometry=geometry,
        crs=crs_label(feature.crs),
        properties=feature.properties,
        area_m2=measurement.area_m2,
        perimeter_m=measurement.perimeter_m,
        length_m=measurement.length_m,
        measurement_crs=measurement.measurement_crs,
        method=measurement.method,
        note=note,
    )


def _has_finite_coordinates(geometry: shapely.Geometry) -> bool:
    # include_z on a 2D geometry would report NaN for the missing Z.
    coords = shapely.get_coordinates(geometry, include_z=geometry.has_z)
    return bool(np.isfinite(coords).all())


def _summarise(features: list[FeatureMeasurement]) -> MeasurementSummary:
    # Every value is already finite (measure_feature guarantees it), and a sum
    # of real-world areas and lengths is nowhere near float overflow.
    return MeasurementSummary(
        geometry_types=dict(Counter(f.geometry_type for f in features if f.geometry_type)),
        without_geometry=sum(1 for f in features if f.geometry_type is None),
        measured=sum(1 for f in features if f.area_m2 is not None or f.length_m is not None),
        total_area_m2=math.fsum(f.area_m2 for f in features if f.area_m2 is not None),
        total_length_m=math.fsum(f.length_m for f in features if f.length_m is not None),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
