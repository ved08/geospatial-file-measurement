from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


class FileStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FileInfo(BaseModel):
    id: str
    filename: str
    feature_count: int | None = None
    crs: str | None = None
    status: FileStatus
    error: str | None = None
    created_at: datetime


class FeatureMeasurement(BaseModel):
    id: int
    geometry_type: str | None
    geometry: dict[str, Any] | None
    crs: str | None
    properties: dict[str, Any]
    measurement_crs: str | None = None
    area_m2: float | None = None
    length_m: float | None = None
    note: str | None = None


class MeasurementsResponse(BaseModel):
    file_id: str
    feature_count: int
    features: list[FeatureMeasurement]
