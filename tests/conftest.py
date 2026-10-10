import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.repository import FileRepository, get_repository, reset_repository


@pytest.fixture(autouse=True)
def repository(tmp_path, monkeypatch) -> FileRepository:
    """Give every test its own empty SQLite database."""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'test.db'}")
    reset_repository()
    repository = get_repository()
    repository.create_schema()
    yield repository
    reset_repository()


@pytest.fixture
def client(tmp_path, monkeypatch, repository):
    # Separate from the database file, which also lives in tmp_path.
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(settings, "upload_dir", upload_dir)
    # As a context manager so the app's lifespan (schema creation, startup recovery) runs.
    with TestClient(app) as client:
        yield client
