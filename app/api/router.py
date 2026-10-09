from fastapi import APIRouter

from app.api.routes import files

api_router = APIRouter(prefix="/api")
api_router.include_router(files.router)
