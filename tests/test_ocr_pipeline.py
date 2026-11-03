"""Tests for the OCR-pending flow, OCR failure reporting and health status."""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pymupdf
import pytest
import httpx

from backend.services import document_parser, ocr_adapter


def _sample_pdf() -> bytes:
    """Page 1: digital text. Page 2: full-page image without text (a scan)."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Digital page with real text")
    scan = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 420))
    pix.clear_with(255)
    scan.insert_image(scan.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def test_run_ocr_false_returns_pending_without_running_ocr(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("OCR must not run when run_ocr=False")

    monkeypatch.setattr(document_parser, "run_ocr_for_page", boom)
    result = document_parser.inspect_pdf(_sample_pdf(), run_ocr=False)

    scan = result["pages"][1]
    digital = result["pages"][0]
    assert digital["has_extractable_text"] is True
    assert digital["source_candidate"] == "digital_candidate"
    assert scan["source_candidate"] == "scan_candidate"
    assert scan["table_extraction_status"] == "ocr_pending"
    assert scan["ocr_applied"] is False
    assert scan["native_text_trusted"] is False
    assert result["scanned_pages_waiting_for_ocr"] == 1
    assert result["ocr_status"] == "pending"
    assert result["pages"][0]["table_extraction_status"] != "ocr_pending"


def test_inspect_progress_counts_completed_pages_and_does_not_expose_page_text():
    progress = []
    result = document_parser.inspect_pdf(
        _sample_pdf(),
        run_ocr=False,
        progress_callback=progress.append,
    )

    assert result["page_count"] == 2
    assert progress[0]["phase"] == "reading_pdf"
    assert any(
        item["phase"] == "extracting_page"
        and item["completed_pages"] == 1
        and item["total_pages"] == 2
        for item in progress
    )
    assert progress[-2]["phase"] == "validating_tables"
    assert progress[-1]["status"] == "completed"
    assert progress[-1]["completed_pages"] == progress[-1]["total_pages"] == 2
    assert all("native_text" not in item and "tables" not in item for item in progress)


def test_single_pdf_runs_independent_scan_page_inference_concurrently(monkeypatch):
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 420))
        pix.clear_with(255)
        page.insert_image(page.rect, pixmap=pix)
    pdf_bytes = doc.tobytes()
    doc.close()

    active = 0
    max_active = 0
    active_lock = threading.Lock()
    rendezvous = threading.Barrier(2)

    monkeypatch.setattr(document_parser, "ocr_dependencies_available", lambda: True)
    monkeypatch.setattr(
        document_parser, "prepare_ocr_page",
        lambda page, **kwargs: {
            "page_number": page.number + 1, "render_elapsed": 0.01,
            "table_image_bytes": None, "temp_path": None,
            "cache_hit": False, "deskew_angle": 0.0, "dpi": 150,
        },
    )

    def infer(prepared):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            rendezvous.wait(timeout=5)
            time.sleep(0.02)
            return {
                "entries": [], "image_width": 1, "image_height": 1,
                "elapsed": 0.02, "cache_hit": False,
            }
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(document_parser, "infer_prepared_ocr", infer)
    monkeypatch.setattr(document_parser, "discard_prepared_ocr", lambda prepared: None)
    monkeypatch.setattr(
        document_parser, "build_ocr_page_proxy",
        lambda page, prepared, result: SimpleNamespace(
            rect=page.rect, render_elapsed_seconds=prepared["render_elapsed"],
            ocr_cache_hit=result["cache_hit"], deskew_angle_degrees=0.0,
            ocr_word_count=0, ocr_elapsed_seconds=result["elapsed"],
            ocr_inference_elapsed_seconds=result["elapsed"],
        ),
    )
    monkeypatch.setattr(
        document_parser, "extract_tables_from_page",
        lambda page, timing_metrics=None: {
            "status": "no_tables", "tables": [], "source_table_count": 0,
            "metadata_items": [], "document_metadata_table": None,
            "summary_items": [], "methods_attempted": [], "methods_failed": [],
        },
    )

    progress = []
    result = document_parser.inspect_pdf(pdf_bytes, run_ocr=True, progress_callback=progress.append)

    assert max_active == 2
    assert result["ocr_pages_completed"] == 2
    assert [page["page_number"] for page in result["pages"]] == [1, 2]
    assert all(page["ocr_applied"] for page in result["pages"])
    ocr_progress = [item for item in progress if item["phase"] == "ocr_inference"]
    assert any(item.get("ocr_pages_processed") == 1 for item in ocr_progress)
    assert all(0 < item["progress_percent"] < 100 for item in ocr_progress if "progress_percent" in item)
    assert progress[-1]["progress_percent"] == 100
    assert progress[-1]["work_units_completed"] == progress[-1]["work_units_total"]


def test_missing_ocr_packages_gives_clear_error(monkeypatch):
    monkeypatch.setattr(document_parser, "ocr_dependencies_available", lambda: False)
    result = document_parser.inspect_pdf(_sample_pdf(), run_ocr=True)

    scan = result["pages"][1]
    assert scan["table_extraction_status"] == "ocr_failed"
    assert "rapidocr" in scan["ocr_error"]
    assert result["ocr_status"] == "partial_failure"


def test_engine_load_failure_is_cached_not_retried(monkeypatch):
    import rapidocr

    calls = {"n": 0}

    def failing_engine(*_a, **_k):
        calls["n"] += 1
        raise OSError("download blocked")

    monkeypatch.setattr(rapidocr, "RapidOCR", failing_engine)
    monkeypatch.setattr(ocr_adapter, "_ENGINE", None)
    monkeypatch.setattr(ocr_adapter, "_ENGINE_FAILURE", None)

    with pytest.raises(RuntimeError, match="Model OCR gagal dimuat"):
        ocr_adapter._get_engine()
    with pytest.raises(RuntimeError, match="Model OCR gagal dimuat"):
        ocr_adapter._get_engine()
    assert calls["n"] == 1  # second call used the cached failure


def test_engine_uses_bounded_onnx_cpu_thread_settings(monkeypatch):
    import rapidocr

    from backend.core.config import settings

    captured = {}
    engine = object()

    def fake_engine(*, params):
        captured.update(params)
        return engine

    monkeypatch.setattr(rapidocr, "RapidOCR", fake_engine)
    monkeypatch.setattr(ocr_adapter, "_ENGINE", None)
    monkeypatch.setattr(ocr_adapter, "_ENGINE_FAILURE", None)

    assert ocr_adapter._get_engine() is engine
    assert captured["EngineConfig.onnxruntime.intra_op_num_threads"] == (
        settings.ocr_intra_op_threads
    )
    assert captured["EngineConfig.onnxruntime.inter_op_num_threads"] == 1


def test_warmup_never_raises_and_reports_failure(monkeypatch):
    def broken():
        raise RuntimeError("no models")

    monkeypatch.setattr(ocr_adapter, "_ENGINE", None)
    monkeypatch.setattr(ocr_adapter, "_WARMUP", {"state": "idle", "error": None, "seconds": None})
    monkeypatch.setattr(ocr_adapter, "_get_engine", broken)
    state = ocr_adapter.warmup_engine()
    assert state["state"] == "failed"
    assert "no models" in state["error"]


def test_warmup_initializes_all_lazy_ocr_sessions_once(monkeypatch):
    calls = []
    engine = SimpleNamespace(
        _load_det_model=lambda: calls.append("det"),
        _load_cls_model=lambda: calls.append("cls"),
        _load_rec_model=lambda: calls.append("rec"),
    )
    monkeypatch.setattr(ocr_adapter, "_WARMUP", {"state": "idle", "error": None, "seconds": None})
    monkeypatch.setattr(ocr_adapter, "_get_engine", lambda: engine)

    first = ocr_adapter.warmup_engine()
    second = ocr_adapter.warmup_engine()

    assert first["state"] == second["state"] == "ready"
    assert calls == ["det", "cls", "rec"]


@pytest.mark.parametrize("fail_inference", [False, True])
def test_ocr_temporary_image_is_removed_after_success_or_failure(monkeypatch, fail_inference):
    captured = {}

    def engine(image_path, *, return_word_box):
        captured["path"] = Path(image_path)
        assert captured["path"].is_file()
        assert return_word_box is True
        if fail_inference:
            raise RuntimeError("synthetic inference failure")
        return SimpleNamespace(word_results=[[
            ("word", 0.99, [[10, 10], [50, 10], [50, 30], [10, 30]])
        ]])

    monkeypatch.setattr(ocr_adapter, "_get_engine", lambda: engine)
    document = pymupdf.open()
    try:
        page = document.new_page()
        if fail_inference:
            with pytest.raises(RuntimeError, match="synthetic inference failure"):
                ocr_adapter.run_ocr_for_page(page, dpi=72)
        else:
            result = ocr_adapter.run_ocr_for_page(page, dpi=72)
            assert result.get_text("text") == "word"
    finally:
        document.close()

    assert captured["path"].exists() is False


def test_run_ocr_allows_two_calls_and_queues_a_third(monkeypatch):
    active = 0
    maximum = 0
    lock = threading.Lock()
    two_started = threading.Event()
    release = threading.Event()

    def engine(_image_path, *, return_word_box):
        nonlocal active, maximum
        assert return_word_box is True
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_started.set()
        try:
            assert release.wait(timeout=3)
            return SimpleNamespace(word_results=[[
                ("word", 0.99, [[10, 10], [50, 10], [50, 30], [10, 30]])
            ]])
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ocr_adapter, "_get_engine", lambda: engine)
    documents = [pymupdf.open() for _ in range(3)]
    pages = [document.new_page() for document in documents]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(ocr_adapter.run_ocr_for_page, page, 72) for page in pages]
        try:
            assert two_started.wait(timeout=2)
            assert maximum == 2
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    for document in documents:
        document.close()

    assert maximum == 2
    assert all(result.get_text("text") == "word" for result in results)


def test_ocr_raw_cache_skips_render_and_inference_for_same_pdf_page(monkeypatch, tmp_path):
    calls = {"inference": 0}

    class ObservedLock:
        active = False

        def __enter__(self):
            assert self.active is False
            self.active = True

        def __exit__(self, *_args):
            self.active = False

    inference_semaphore = ObservedLock()

    def engine(image_path, *, return_word_box):
        calls["inference"] += 1
        assert inference_semaphore.active is True
        assert Path(image_path).is_file()
        assert return_word_box is True
        return SimpleNamespace(word_results=[[
            ("cached word", 0.97, [[10, 10], [60, 10], [60, 30], [10, 30]])
        ]])

    def load_engine():
        assert inference_semaphore.active is False
        return engine

    monkeypatch.setattr(ocr_adapter, "_OCR_CACHE_DIR", tmp_path / "ocr-cache")
    monkeypatch.setattr(ocr_adapter, "_OCR_INFERENCE_SEMAPHORE", inference_semaphore)
    monkeypatch.setattr(ocr_adapter, "_get_engine", load_engine)
    source = pymupdf.open()
    page = source.new_page()
    first = ocr_adapter.run_ocr_for_page(
        page, dpi=72, cache_key="same-document-digest", page_number=1
    )
    second = ocr_adapter.run_ocr_for_page(
        page, dpi=72, cache_key="same-document-digest", page_number=1
    )
    source.close()

    assert calls["inference"] == 1
    assert first.ocr_cache_hit is False
    assert second.ocr_cache_hit is True
    assert second.render_elapsed_seconds == 0
    assert second.get_text("text") == "cached word"


def test_ocr_cache_key_separates_pages_and_dpi(monkeypatch, tmp_path):
    calls = {"inference": 0}

    def engine(_image_path, *, return_word_box):
        calls["inference"] += 1
        return SimpleNamespace(word_results=[[
            ("page text", 0.9, [[10, 10], [50, 10], [50, 30], [10, 30]])
        ]])

    monkeypatch.setattr(ocr_adapter, "_OCR_CACHE_DIR", tmp_path / "ocr-cache")
    monkeypatch.setattr(ocr_adapter, "_get_engine", lambda: engine)
    source = pymupdf.open()
    page = source.new_page()
    for number, dpi in [(1, 72), (2, 72), (1, 150)]:
        ocr_adapter.run_ocr_for_page(
            page, dpi=dpi, cache_key="same-document-digest", page_number=number
        )
    source.close()

    assert calls["inference"] == 3


def test_endpoint_ocr_flag_and_health(live_http_server):
    response = httpx.post(
        f"{live_http_server}/api/documents/inspect?ocr=false",
        files={"file": ("sample.pdf", _sample_pdf(), "application/pdf")},
        timeout=30,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ocr_requested"] is False
    assert body["pages"][1]["table_extraction_status"] == "ocr_pending"

    health = httpx.get(f"{live_http_server}/api/health", timeout=5).json()
    assert health["status"] == "ok"
    assert "dependencies_installed" in health["ocr"]


def test_endpoint_reports_page_progress_without_document_content(live_http_server):
    progress_id = uuid4()
    response = httpx.post(
        f"{live_http_server}/api/documents/inspect?ocr=false&progress_id={progress_id}",
        files={"file": ("progress-sample.pdf", _sample_pdf(), "application/pdf")},
        timeout=30,
    )
    assert response.status_code == 200

    progress_response = httpx.get(
        f"{live_http_server}/api/documents/inspect/progress/{progress_id}", timeout=5
    )
    assert progress_response.status_code == 200
    assert progress_response.headers["cache-control"] == "no-store"
    progress = progress_response.json()
    assert progress["status"] == "completed"
    assert progress["phase"] == "completed"
    assert progress["completed_pages"] == progress["total_pages"] == 2
    assert progress["failed_pages"] == 0
    assert "pages" not in progress
    assert "native_text" not in progress


def test_invalid_pdf_upload_sets_failed_progress(live_http_server):
    progress_id = uuid4()
    response = httpx.post(
        f"{live_http_server}/api/documents/inspect?ocr=false&progress_id={progress_id}",
        files={"file": ("broken.pdf", b"not a pdf", "application/pdf")},
        timeout=30,
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]

    progress = httpx.get(
        f"{live_http_server}/api/documents/inspect/progress/{progress_id}", timeout=5
    )
    assert progress.status_code == 200
    assert progress.json()["status"] == "failed"
    assert progress.json()["phase"] == "failed"
    assert progress.json()["failed_pages"] == 1
