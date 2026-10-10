"""API models for uploaded files and their measurements.

Records are frozen: the repository replaces them instead of mutating them, so
a request handler can never see one half-updated by the background task.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FileStatus(str, Enum):
    PENDING = "PENDING"  # uploaded and validated, waiting for processing to start
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"  # see FileInfo.error


class FileInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    filename: str
    feature_count: int | None = None  # set once processing has completed
    crs: str | None = None  # e.g. "EPSG:4326"; None if the layers disagree or there are no features
    status: FileStatus
    error: str | None = None  # why processing failed
    warnings: list[str] = Field(default_factory=list)  # non-fatal issues found while reading
    created_at: datetime
    processed_at: datetime | None = None  # when processing completed or failed


class FeatureMeasurement(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int  # position in the file across all layers, starting at 0
    layer: str | None = None
    geometry_type: str | None  # None if the feature has no geometry
    geometry: dict[str, Any] | None  # GeoJSON, in the file's own CRS
    crs: str | None
    properties: dict[str, Any]
    area_m2: float | None = None  # polygons
    perimeter_m: float | None = None  # polygons, including the edges of holes
    length_m: float | None = None  # lines
    measurement_crs: str | None = None  # CRS the measurement was made in, e.g. "EPSG:32643"
    method: str | None = None  # "projected" or "geodesic"
    note: str | None = None  # why nothing was measured, or that the geometry was repaired


class MeasurementSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    geometry_types: dict[str, int]  # feature count per geometry type
    without_geometry: int
    measured: int  # features with an area or a length
    total_area_m2: float
    total_length_m: float


class MeasurementsResponse(BaseModel):
    file_id: str
    crs: str | None
    total: int  # number of features in the file; `features` holds one page of them
    offset: int
    limit: int | None
    summary: MeasurementSummary  # covers every feature, not just this page
    features: list[FeatureMeasurement]
