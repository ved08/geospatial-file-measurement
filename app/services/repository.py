"""In-memory store for uploaded file records.

Placeholder until a database is introduced; keep the interface small so it
can be swapped for a SQL-backed implementation without touching the routes.
"""

from app.schemas.files import FeatureMeasurement, FileInfo


class FileRepository:
    def __init__(self) -> None:
        self._files: dict[str, FileInfo] = {}
        self._measurements: dict[str, list[FeatureMeasurement]] = {}

    def add(self, file: FileInfo) -> None:
        self._files[file.id] = file
        self._measurements[file.id] = []

    def get(self, file_id: str) -> FileInfo | None:
        return self._files.get(file_id)

    def update(self, file: FileInfo) -> None:
        self._files[file.id] = file

    def set_measurements(self, file_id: str, features: list[FeatureMeasurement]) -> None:
        self._measurements[file_id] = features

    def get_measurements(self, file_id: str) -> list[FeatureMeasurement]:
        return self._measurements.get(file_id, [])


repository = FileRepository()
