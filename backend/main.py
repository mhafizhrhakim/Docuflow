
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes.health import router as health_router
from backend.api.routes.documents import router as documents_router
from backend.core.config import settings
from backend.services.ocr_adapter import ocr_dependencies_available, warmup_engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load OCR models in the background at startup so the first upload does not
    # pay the model-loading (and first-run download) cost.
    if ocr_dependencies_available():
        threading.Thread(target=warmup_engine, name="ocr-warmup", daemon=True).start()
    yield

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Local-first API for inspecting PDF pages "
        "in the DocuFlow document extraction workspace."
    ),
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# The frontend and API normally share one origin behind the deployment proxy.
# Keep a strict explicit CORS allowlist for approved split-origin deployments.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(health_router, prefix="/api")
app.include_router(documents_router, prefix="/api")


@app.get("/", tags=["system"])
def root() -> dict[str, str]:
    return {
        "app_name": settings.app_name,
        "status": "ok",
        "message": "DocuFlow API is running.",
    }
