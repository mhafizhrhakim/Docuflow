"""PDF inspection API endpoint for DocuFlow."""

from typing import Any
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import logging
import threading
import time
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from backend.core.config import settings
from backend.services.document_parser import inspect_pdf
from backend.services.comparison_service import compare_extracted_documents

router = APIRouter(tags=["documents"])
MAX_PDF_SIZE_BYTES = max(1, settings.max_upload_mb) * 1024 * 1024
_INSPECTION_CAPACITY = threading.BoundedSemaphore(
    settings.inspection_workers + settings.inspection_queue_slots
)
_PROGRESS_LOCK = threading.Lock()
_PROGRESS_TTL_SECONDS = max(1, settings.upload_ttl_minutes) * 60
_PROGRESS: dict[str, tuple[float, dict[str, Any]]] = {}
_INSPECTION_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.inspection_workers,
    thread_name_prefix="docuflow-inspect",
)


class ComparisonRequest(BaseModel):
    documents: list[dict[str, Any]] = Field(min_length=2, max_length=20)


def _store_progress(progress_id: UUID, state: dict[str, Any]) -> None:
    now = time.monotonic()
    with _PROGRESS_LOCK:
        expired = [key for key, (updated_at, _value) in _PROGRESS.items()
                   if now - updated_at > _PROGRESS_TTL_SECONDS]
        for key in expired:
            _PROGRESS.pop(key, None)
        _PROGRESS[str(progress_id)] = (now, dict(state))


def _current_progress(progress_id: UUID) -> dict[str, Any]:
    with _PROGRESS_LOCK:
        item = _PROGRESS.get(str(progress_id))
        return dict(item[1]) if item is not None else {}


@router.post("/documents/inspect")
async def inspect_document(
    file: UploadFile = File(...),
    ocr: bool = Query(
        True,
        description="false = hanya ekstraksi halaman digital; halaman scan dikembalikan sebagai ocr_pending.",
    ),
    progress_id: UUID | None = Query(
        None,
        description="Optional opaque ID used by the local UI to poll page progress.",
    ),
) -> dict[str, Any]:
    """Inspect PDF pages without permanently storing the uploaded file."""
    filename = (file.filename or "").strip()

    if not filename.lower().endswith(".pdf"):
        await file.close()
        raise HTTPException(status_code=400, detail="File harus berformat PDF.")

    # Keep the executor queue bounded. Requests beyond the configured
    # active+queued capacity receive an explicit retryable response rather
    # than accumulating PDF bytes and waiting without limit in memory.
    if not _INSPECTION_CAPACITY.acquire(blocking=False):
        await file.close()
        raise HTTPException(
            status_code=503,
            detail="Kapasitas ekstraksi sedang penuh. Coba unggah kembali setelah proses lain selesai.",
        )

    capacity_transferred_to_worker = False
    try:
        try:
            pdf_bytes = await file.read(MAX_PDF_SIZE_BYTES + 1)
        finally:
            await file.close()

        if len(pdf_bytes) > MAX_PDF_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Ukuran PDF melebihi batas {max(1, settings.max_upload_mb)} MB.",
            )

        def report_progress(state: dict[str, Any]) -> None:
            if progress_id is not None:
                _store_progress(progress_id, state)

        if progress_id is not None:
            report_progress({
                "phase": "queued",
                "status": "waiting",
                "completed_pages": 0,
                "total_pages": None,
                "current_page": None,
                "failed_pages": 0,
            })

        try:
            # Keep PDF parsing/OCR off the ASGI event loop. Shared OCR
            # inference remains separately bounded by the OCR adapter.
            loop = asyncio.get_running_loop()
            inspection_future = loop.run_in_executor(
                _INSPECTION_EXECUTOR,
                partial(
                    inspect_pdf,
                    pdf_bytes,
                    run_ocr=ocr,
                    progress_callback=report_progress if progress_id is not None else None,
                ),
            )
            try:
                result = await asyncio.shield(inspection_future)
            except asyncio.CancelledError:
                # A Python worker cannot be force-stopped safely. Keep its
                # admission slot until it actually finishes, even if the HTTP
                # client disconnects or cancels its request.
                capacity_transferred_to_worker = True
                inspection_future.add_done_callback(
                    lambda _future: _INSPECTION_CAPACITY.release()
                )
                if progress_id is not None:
                    cancelled_progress = _current_progress(progress_id)
                    cancelled_progress.update(phase="failed", status="failed")
                    report_progress(cancelled_progress)
                raise
        except ValueError as exc:
            if progress_id is not None:
                failed_progress = _current_progress(progress_id)
                failed_progress.update(phase="failed", status="failed", failed_pages=max(1, int(failed_progress.get("failed_pages") or 0)))
                report_progress(failed_progress)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            if progress_id is not None:
                failed_progress = _current_progress(progress_id)
                failed_progress.update(phase="failed", status="failed", failed_pages=max(1, int(failed_progress.get("failed_pages") or 0)))
                report_progress(failed_progress)
            logging.getLogger(__name__).exception("Unexpected PDF inspection failure")
            raise HTTPException(
                status_code=500,
                detail="PDF gagal diperiksa karena kesalahan internal.",
            ) from exc

        return {
            "filename": filename,
            "size_bytes": len(pdf_bytes),
            "ocr_requested": ocr,
            **result,
        }
    finally:
        # Covers oversize PDFs, parser errors, cancellation, and successful
        # inspection so a failed request cannot permanently consume a slot.
        # A still-running worker owns the slot after client cancellation.
        if not capacity_transferred_to_worker:
            _INSPECTION_CAPACITY.release()


@router.get("/documents/inspect/progress/{progress_id}")
def inspect_progress(progress_id: UUID, response: Response) -> dict[str, Any]:
    """Return numeric progress only; it never contains document text or tables."""
    response.headers["Cache-Control"] = "no-store"
    now = time.monotonic()
    with _PROGRESS_LOCK:
        expired = [key for key, (updated_at, _value) in _PROGRESS.items()
                   if now - updated_at > _PROGRESS_TTL_SECONDS]
        for key in expired:
            _PROGRESS.pop(key, None)
        item = _PROGRESS.get(str(progress_id))
        if item is None:
            raise HTTPException(status_code=404, detail="Progress tidak ditemukan atau kedaluwarsa.")
        return dict(item[1])


@router.post("/documents/compare")
def compare_documents(payload: ComparisonRequest) -> dict[str, Any]:
    """Compare already-extracted document results without re-running OCR."""
    return compare_extracted_documents(payload.documents)
