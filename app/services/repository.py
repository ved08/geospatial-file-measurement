"""SQL-backed store for uploaded file records and their measurements.

The interface is small and query-shaped (get / update / page of measurements /
count) and speaks only the Pydantic schemas: ORM rows and sessions never leave
this module, so routes and the pipeline don't know a database is involved.

Each method runs in its own short transaction, so no application-level lock is
needed: the database isolates concurrent readers and writers (the background
thread writes while request handlers read), and a reader only ever sees
committed data. Callers that need an ordering, like the pipeline
storing measurements before marking a file COMPLETED, get it by making the
calls in that order: each one has committed by the time it returns.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, delete, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db import create_db_engine
from app.models import Base, FeatureRow, FileRow
from app.schemas.files import FeatureMeasurement, FileInfo, FileStatus, MeasurementSummary

_FILE_COLUMNS = tuple(FileInfo.model_fields)
_UPDATABLE_FILE_COLUMNS = frozenset(_FILE_COLUMNS) - {"id"}
_FEATURE_COLUMNS = tuple(name for name in FeatureMeasurement.model_fields if name != "id")


class FileRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # Rows are copied into Pydantic models before the session closes, so
        # there's nothing to gain from expiring (and reloading) them on commit.
        self._sessions: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> "FileRepository":
        return cls(create_db_engine(url))

    def create_schema(self) -> None:
        """Create any missing tables (idempotent)."""
        Base.metadata.create_all(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    # --- files -------------------------------------------------------------

    def add(self, file: FileInfo) -> None:
        with self._sessions.begin() as session:
            session.add(FileRow(**file.model_dump(include=set(_FILE_COLUMNS))))

    def get(self, file_id: str) -> FileInfo | None:
        with self._sessions() as session:
            row = session.get(FileRow, file_id)
            return None if row is None else _file_info(row)

    def update(self, file_id: str, **changes: Any) -> FileInfo:
        """Apply `changes` to the record and return the updated copy.

        Raises KeyError if there is no such file. Changes are validated against
        FileInfo first, so a bad field name or value never reaches the database.
        """
        if invalid := changes.keys() - _UPDATABLE_FILE_COLUMNS:
            raise ValueError(f"Cannot update fields: {sorted(invalid)}")
        with self._sessions.begin() as session:
            row = session.get(FileRow, file_id)
            if row is None:
                raise KeyError(file_id)
            updated = FileInfo.model_validate(_file_info(row).model_dump() | changes)
            for name in changes:
                setattr(row, name, getattr(updated, name))
            return updated

    def delete(self, file_id: str) -> bool:
        """Delete a file record; its feature rows go with it (ON DELETE CASCADE)."""
        with self._sessions.begin() as session:
            result = session.execute(delete(FileRow).where(FileRow.id == file_id))
            return result.rowcount > 0

    def fail_interrupted(self, error: str, processed_at: datetime) -> list[str]:
        """Mark every PENDING/PROCESSING file as FAILED and return their ids.

        For startup: background tasks don't survive a restart, so any file still
        in one of these states will never finish.
        """
        unfinished = FileRow.status.in_([FileStatus.PENDING, FileStatus.PROCESSING])
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(FileRow.id).where(unfinished)))
            if ids:
                session.execute(
                    update(FileRow)
                    .where(FileRow.id.in_(ids), unfinished)
                    .values(status=FileStatus.FAILED, error=error, processed_at=processed_at)
                )
            return ids

    # --- measurements ------------------------------------------------------

    def set_measurements(
        self, file_id: str, features: Sequence[FeatureMeasurement], summary: MeasurementSummary
    ) -> None:
        """Replace the file's measurements and summary in one transaction.

        Idempotent: existing rows are deleted first, so re-processing a file
        doesn't fail on duplicate keys or leave stale features behind.
        """
        rows = [
            {"file_id": file_id, "feature_index": f.id, **f.model_dump(include=set(_FEATURE_COLUMNS))}
            for f in features
        ]
        with self._sessions.begin() as session:
            file_exists = session.execute(
                update(FileRow).where(FileRow.id == file_id).values(summary=summary.model_dump())
            ).rowcount
            if not file_exists:
                raise KeyError(file_id)
            session.execute(delete(FeatureRow).where(FeatureRow.file_id == file_id))
            if rows:
                # ORM bulk INSERT: one executemany (batched multi-row INSERTs)
                # rather than an ORM object per feature. render_nulls keeps None
                # values in the statement; by default they're left out, which
                # splits the rows into a batch per distinct set of NULL columns.
                session.execute(insert(FeatureRow).execution_options(render_nulls=True), rows)

    def get_measurements(
        self, file_id: str, offset: int = 0, limit: int | None = None
    ) -> list[FeatureMeasurement]:
        query = (
            select(FeatureRow)
            .where(FeatureRow.file_id == file_id)
            .order_by(FeatureRow.feature_index)
            .offset(offset)
            .limit(limit)
        )
        with self._sessions() as session:
            return [_feature(row) for row in session.scalars(query)]

    def count_measurements(self, file_id: str) -> int:
        query = select(func.count()).select_from(FeatureRow).where(FeatureRow.file_id == file_id)
        with self._sessions() as session:
            return session.scalar(query) or 0

    def get_summary(self, file_id: str) -> MeasurementSummary | None:
        with self._sessions() as session:
            summary = session.scalar(select(FileRow.summary).where(FileRow.id == file_id))
            return None if summary is None else MeasurementSummary.model_validate(summary)


def _file_info(row: FileRow) -> FileInfo:
    return FileInfo.model_validate({name: getattr(row, name) for name in _FILE_COLUMNS})


def _feature(row: FeatureRow) -> FeatureMeasurement:
    return FeatureMeasurement.model_validate(
        {"id": row.feature_index, **{name: getattr(row, name) for name in _FEATURE_COLUMNS}}
    )


# --- the app's repository --------------------------------------------------------

_repository: FileRepository | None = None


def get_repository() -> FileRepository:
    """Return the repository for `settings.database_url`, creating it on first use.

    Routes get it through `Depends(get_repository)` (overridable in tests), and
    the pipeline falls back to it, so a task queue worker process gets its own
    engine just by calling this.
    """
    # Not locked: the app's lifespan calls this once at startup, before any
    # request or background task can race it.
    global _repository
    if _repository is None:
        _repository = FileRepository.from_url(settings.database_url)
    return _repository


def reset_repository() -> None:
    """Dispose of the current repository; the next get_repository() builds a new one.

    Used at shutdown and by tests that point `settings.database_url` elsewhere.
    """
    global _repository
    if _repository is not None:
        _repository.close()
        _repository = None
