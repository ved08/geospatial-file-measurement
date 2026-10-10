from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.middleware import MaxBodySizeMiddleware
from app.api.router import api_router
from app.core.config import settings
from app.services.pipeline import fail_interrupted_files
from app.services.repository import get_repository, reset_repository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Blocking DB calls, but nothing is being served yet, so the event loop has
    # nothing else to do.
    repository = get_repository()
    repository.create_schema()
    fail_interrupted_files(repository)
    yield
    reset_repository()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(MaxBodySizeMiddleware)
app.include_router(api_router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
