from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pyproj import CRS
from pyproj.exceptions import CRSError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GEO_")

    app_name: str = "Geospatial File Measurement API"
    upload_dir: Path = Path("uploads")
    # SQLAlchemy URL. The default SQLite file (relative to the working directory)
    # needs no server; e.g. GEO_DATABASE_URL=postgresql+psycopg://user:pw@host/db
    # would work too once a driver (psycopg) is installed.
    database_url: str = "sqlite:///./data/app.db"
    max_upload_size_mb: int = 50
    # Limits applied to .zip contents to guard against zip bombs.
    max_extracted_size_mb: int = 500
    max_zip_entries: int = 1000
    # CRS to assume when a file doesn't define one (e.g. a shapefile without .prj).
    # Unset by default: such files fail, because guessing can give silently wrong areas.
    default_crs: str | None = None

    @field_validator("default_crs")
    @classmethod
    def _validate_default_crs(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                CRS.from_user_input(value)
            except CRSError as exc:
                raise ValueError(f"Invalid default_crs {value!r}: {exc}") from exc
        return value


settings = Settings()
