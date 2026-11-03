from typing import Any

from fastapi import APIRouter

from backend.core.config import settings
from backend.services.ocr_adapter import get_ocr_runtime_status

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "environment": settings.app_env,
        # Quick check that the running backend can really do OCR:
        # dependencies_installed must be true and engine_state "ready".
        "ocr": get_ocr_runtime_status(),
    }
