import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import StatementError

from app.core.config import settings
from app.db import create_db_engine
from app.main import app
from app.schemas.files import FeatureMeasurement, FileInfo, FileStatus, MeasurementSummary
from app.services.pipeline import INTERRUPTED_MESSAGE, fail_interrupted_files
from app.services.repository import FileRepository

CREATED = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
SUMMARY = MeasurementSummary(
    geometry_types={"Polygon": 1}, without_geometry=1, measured=1, total_area_m2=12.5, total_length_m=0
)


def add_file(repository, file_id="f1", status=FileStatus.PENDING, **fields):
    file = FileInfo(id=file_id, filename="farms.zip", status=status, created_at=CREATED, **fields)
    repository.add(file)
    return file


def feature(index, **fields):
    defaults = {
        "geometry_type": "Point",
        "geometry": {"type": "Point", "coordinates": [77.59, 12.97]},
        "crs": "EPSG:4326",
        "properties": {"name": f"f{index}"},
    }
    return FeatureMeasurement(id=index, **(defaults | fields))


# --- files ---------------------------------------------------------------------


def test_file_round_trip(repository):
    file = add_file(
        repository, status=FileStatus.COMPLETED, feature_count=3, crs="EPSG:4326",
        warnings=["Layer 'x' is empty", "ünïcode ⚠"], processed_at=CREATED + timedelta(seconds=2),
    )
    assert repository.get("f1") == file
    assert repository.get("missing") is None


def test_datetimes_come_back_as_utc(repository):
    ist = timezone(timedelta(hours=5, minutes=30))
    repository.add(FileInfo(id="f1", filename="a.kml", status=FileStatus.PENDING, created_at=CREATED.astimezone(ist)))
    created_at = repository.get("f1").created_at
    assert created_at == CREATED
    assert created_at.utcoffset() == timedelta(0)


def test_update(repository):
    add_file(repository)
    updated = repository.update("f1", status=FileStatus.FAILED, error="bad file", processed_at=CREATED)
    assert (updated.status, updated.error, updated.processed_at) == (FileStatus.FAILED, "bad file", CREATED)
    assert repository.get("f1") == updated
    assert updated.filename == "farms.zip"  # untouched fields are kept


def test_update_rejects_unknown_files_fields_and_values(repository):
    add_file(repository)
    with pytest.raises(KeyError):
        repository.update("missing", status=FileStatus.FAILED)
    with pytest.raises(ValueError, match="nope"):
        repository.update("f1", nope=1)
    with pytest.raises(ValueError, match="id"):
        repository.update("f1", id="f2")
    with pytest.raises(ValidationError):
        repository.update("f1", status="BOGUS")
    assert repository.get("f1").status is FileStatus.PENDING


def test_delete_cascades_to_features(repository):
    add_file(repository)
    add_file(repository, "f2")
    repository.set_measurements("f1", [feature(0), feature(1)], SUMMARY)
    repository.set_measurements("f2", [feature(0)], SUMMARY)

    assert repository.delete("f1") is True
    assert repository.get("f1") is None
    assert repository.count_measurements("f1") == 0
    assert repository.count_measurements("f2") == 1
    assert repository.delete("f1") is False


# --- measurements --------------------------------------------------------------


def test_measurements_round_trip_json_values(repository):
    add_file(repository)
    properties = {
        "name": "Bengaluru – ಬೆಂಗಳೂರು 🌳",
        "nested": {"list": [1, 2.5, None, {"deep": True}], "empty": {}},
        "missing": None,
        "count": 0,
    }
    polygon = {
        "type": "Polygon",
        "coordinates": [[[77.59, 12.97], [77.6, 12.97], [77.6, 12.98], [77.59, 12.97]]],
    }
    features = [
        feature(
            0, layer="farms", geometry_type="Polygon", geometry=polygon, properties=properties,
            area_m2=1_201_684.25, perimeter_m=4_385.5, measurement_crs="EPSG:32643", method="projected",
        ),
        feature(1, geometry_type=None, geometry=None, crs=None, properties={}, note="Feature has no geometry"),
    ]
    repository.set_measurements("f1", features, SUMMARY)

    assert repository.get_measurements("f1") == features
    assert repository.get_summary("f1") == SUMMARY


def test_pagination_follows_feature_order(repository):
    add_file(repository)
    add_file(repository, "other")
    # Inserted out of order; pages come back ordered by feature index.
    repository.set_measurements("f1", [feature(i) for i in (3, 0, 4, 1, 2)], SUMMARY)
    repository.set_measurements("other", [feature(i) for i in range(10)], SUMMARY)

    def ids(**page):
        return [f.id for f in repository.get_measurements("f1", **page)]

    assert ids() == [0, 1, 2, 3, 4]
    assert ids(offset=1, limit=2) == [1, 2]
    assert ids(offset=3) == [3, 4]
    assert ids(limit=1) == [0]
    assert ids(offset=5) == []
    assert repository.count_measurements("f1") == 5
    assert repository.count_measurements("other") == 10


def test_no_measurements_yet(repository):
    add_file(repository)
    assert repository.get_measurements("f1") == []
    assert repository.count_measurements("f1") == 0
    assert repository.get_summary("f1") is None
    assert repository.count_measurements("missing") == 0
    assert repository.get_summary("missing") is None


def test_set_measurements_replaces_previous_ones(repository):
    add_file(repository)
    repository.set_measurements("f1", [feature(i) for i in range(5)], SUMMARY)
    new_summary = SUMMARY.model_copy(update={"measured": 0})
    repository.set_measurements("f1", [feature(0, note="again"), feature(1)], new_summary)

    assert [f.note for f in repository.get_measurements("f1")] == ["again", None]
    assert repository.count_measurements("f1") == 2
    assert repository.get_summary("f1") == new_summary


def test_set_measurements_is_atomic(repository):
    add_file(repository)
    repository.set_measurements("f1", [feature(0)], SUMMARY)
    # NaN isn't valid JSON: the insert fails, and the delete and summary update
    # in the same transaction are rolled back with it.
    broken = [feature(0), feature(1, properties={"x": float("nan")})]
    with pytest.raises(StatementError, match="not JSON compliant"):
        repository.set_measurements("f1", broken, SUMMARY.model_copy(update={"measured": 99}))

    assert repository.get_measurements("f1") == [feature(0)]
    assert repository.get_summary("f1") == SUMMARY


def test_set_measurements_for_unknown_file(repository):
    with pytest.raises(KeyError):
        repository.set_measurements("missing", [feature(0)], SUMMARY)
    assert repository.count_measurements("missing") == 0


# --- startup recovery ----------------------------------------------------------


def test_fail_interrupted_files(repository):
    for status in FileStatus:
        add_file(repository, status.value, status=status)

    assert sorted(fail_interrupted_files(repository)) == ["PENDING", "PROCESSING"]
    for file_id in ("PENDING", "PROCESSING"):
        file = repository.get(file_id)
        assert file.status is FileStatus.FAILED
        assert file.error == INTERRUPTED_MESSAGE
        assert file.processed_at is not None
    assert repository.get("COMPLETED").status is FileStatus.COMPLETED
    assert repository.get("FAILED").error is None
    assert fail_interrupted_files(repository) == []


def test_app_startup_fails_interrupted_files(repository):
    add_file(repository, "stuck", status=FileStatus.PROCESSING)
    with TestClient(app) as client:
        body = client.get("/api/files/stuck/").json()
    assert (body["status"], body["error"]) == ("FAILED", INTERRUPTED_MESSAGE)


# --- the database itself -------------------------------------------------------


def test_data_survives_a_restart(repository):
    add_file(repository, status=FileStatus.COMPLETED, feature_count=2)
    repository.set_measurements("f1", [feature(0), feature(1)], SUMMARY)
    repository.close()

    restarted = FileRepository.from_url(settings.database_url)
    try:
        restarted.create_schema()  # as at startup: must not drop or touch existing data
        assert restarted.get("f1").feature_count == 2
        assert restarted.get_measurements("f1") == [feature(0), feature(1)]
        assert restarted.get_summary("f1") == SUMMARY
    finally:
        restarted.close()


def test_sqlite_parent_directory_is_created(tmp_path):
    repository = FileRepository.from_url(f"sqlite:///{tmp_path / 'nested' / 'dir' / 'app.db'}")
    try:
        repository.create_schema()
        assert (tmp_path / "nested" / "dir" / "app.db").exists()
    finally:
        repository.close()


def test_sqlite_connection_pragmas(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'app.db'}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
    finally:
        engine.dispose()


def test_reads_while_a_background_thread_writes(repository):
    """Smoke test of the real access pattern: one writer thread, concurrent readers."""
    file_ids = [f"f{i}" for i in range(20)]
    errors: list[BaseException] = []

    def writer():
        try:
            for file_id in file_ids:
                add_file(repository, file_id)
                repository.update(file_id, status=FileStatus.PROCESSING)
                repository.set_measurements(file_id, [feature(i) for i in range(200)], SUMMARY)
                repository.update(file_id, status=FileStatus.COMPLETED, feature_count=200)
        except BaseException as exc:
            errors.append(exc)

    def reader():
        try:
            while thread.is_alive():
                for file_id in file_ids:
                    file = repository.get(file_id)
                    # Measurements are committed before COMPLETED is.
                    if file is not None and file.status is FileStatus.COMPLETED:
                        assert repository.count_measurements(file_id) == 200
                        assert len(repository.get_measurements(file_id, 100, 50)) == 50
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for r in readers:
        r.start()
    thread.join()
    for r in readers:
        r.join()

    assert errors == []
    assert all(repository.get(file_id).status is FileStatus.COMPLETED for file_id in file_ids)
