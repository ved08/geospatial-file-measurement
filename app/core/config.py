from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GEO_")

    app_name: str = "Geospatial File Measurement API"
    upload_dir: Path = Path("uploads")
    max_upload_size_mb: int = 50
    # Limits applied to .zip contents to guard against zip bombs.
    max_extracted_size_mb: int = 500
    max_zip_entries: int = 1000


settings = Settings()
