import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pymupdf

from backend.api.routes.documents import _INSPECTION_EXECUTOR


def test_backend_inspection_pool_is_bounded_and_one_job_failure_isolated():
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    both_workers_started = threading.Event()
    release_workers = threading.Event()

    def work(value):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_workers_started.set()
        assert release_workers.wait(timeout=3)
        with state_lock:
            active -= 1
        if value == "bad":
            raise ValueError("one inspection failed")
        return value

    futures = [
        _INSPECTION_EXECUTOR.submit(work, value)
        for value in ("one", "bad", "two")
    ]
    try:
        assert both_workers_started.wait(timeout=2)
        assert max_active == 2
    finally:
        release_workers.set()

    results = []
    failures = []
    for future in futures:
        try:
            results.append(future.result(timeout=3))
        except ValueError as exc:
            failures.append(str(exc))

    assert _INSPECTION_EXECUTOR._max_workers == 2
    assert sorted(results) == ["one", "two"]
    assert failures == ["one inspection failed"]


def _digital_pdf_with_marker(marker: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 96), marker)
    try:
        return document.tobytes()
    finally:
        document.close()


def test_three_concurrent_http_uploads_keep_documents_and_progress_isolated(live_http_server):
    """Exercise three independent uploads through the real local HTTP route."""
    uploads = [
        (
            f"concurrent-{index}.pdf",
            f"DOCUFLOW-REQUEST-{index}",
            _digital_pdf_with_marker(f"DOCUFLOW-REQUEST-{index}"),
            uuid4(),
        )
        for index in range(1, 4)
    ]

    def inspect(upload):
        filename, marker, pdf_bytes, progress_id = upload
        response = httpx.post(
            f"{live_http_server}/api/documents/inspect",
            params={"ocr": "false", "progress_id": str(progress_id)},
            files={"file": (filename, pdf_bytes, "application/pdf")},
            timeout=20,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["filename"] == filename
        assert marker in payload["pages"][0]["native_text"]

        progress_response = httpx.get(
            f"{live_http_server}/api/documents/inspect/progress/{progress_id}",
            timeout=5,
        )
        assert progress_response.status_code == 200
        progress = progress_response.json()
        assert progress["status"] == "completed"
        assert progress["completed_pages"] == progress["total_pages"] == 1
        return filename, marker

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(inspect, uploads))

    assert results == [(upload[0], upload[1]) for upload in uploads]
