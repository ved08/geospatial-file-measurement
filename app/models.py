"""Database tables for uploaded files and their per-feature measurements.

These ORM classes stay inside the repository; the rest of the app works with
the Pydantic schemas in app.schemas.files.

The schema is created with `Base.metadata.create_all` at startup. That only
creates missing tables; changing an existing table would need migrations
(Alembic), which is the next step once the schema has to evolve.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.schemas.files import FileStatus


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    Postgres stores `timestamptz`; SQLite has no timezone support and hands
    back naive values. Values are converted to UTC on the way in and tagged
    as UTC on the way out, so the API always returns `...+00:00`.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime; pass a timezone-aware value")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime,
        dict[str, Any]: JSON(none_as_null=True),  # None is SQL NULL, not JSON null
        list[str]: JSON(none_as_null=True),
    }


class FileRow(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # uuid4 hex
    filename: Mapped[str] = mapped_column(Text)
    # Stored as the enum's name in a VARCHAR (no native ENUM type), so adding a
    # status later doesn't require altering a database type. Indexed for the
    # startup query that finds interrupted files.
    status: Mapped[FileStatus] = mapped_column(
        Enum(FileStatus, native_enum=False, length=16), index=True
    )
    feature_count: Mapped[int | None]
    crs: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[str]] = mapped_column(default=list)
    # MeasurementSummary as JSON; set together with the feature rows.
    summary: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]
    processed_at: Mapped[datetime | None]


class FeatureRow(Base):
    __tablename__ = "features"

    # Composite primary key: its index (file_id, feature_index) is exactly what
    # "WHERE file_id = ? ORDER BY feature_index LIMIT ? OFFSET ?" needs, so a
    # page is read in order without sorting.
    file_id: Mapped[str] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    feature_index: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    layer: Mapped[str | None] = mapped_column(Text)
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    geometry: Mapped[dict[str, Any] | None]  # GeoJSON in the file's own CRS
    crs: Mapped[str | None] = mapped_column(Text)
    properties: Mapped[dict[str, Any]]
    area_m2: Mapped[float | None] = mapped_column(Float)
    perimeter_m: Mapped[float | None] = mapped_column(Float)
    length_m: Mapped[float | None] = mapped_column(Float)
    measurement_crs: Mapped[str | None] = mapped_column(String(64))
    method: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
