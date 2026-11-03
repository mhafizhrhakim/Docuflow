"""Inspect PDF pages, extracting native text/tables and OCR for scan pages."""
from __future__ import annotations

import logging
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import pymupdf

from backend.services.ocr_adapter import (
    build_ocr_page_proxy,
    discard_prepared_ocr,
    infer_prepared_ocr,
    ocr_dependencies_available,
    prepare_ocr_page,
    run_ocr_for_page,  # compatibility for existing test and integration hooks
)

OCR_NOT_INSTALLED_MESSAGE = (
    "Paket OCR (rapidocr, onnxruntime) belum terpasang di environment backend yang sedang berjalan. "
    "Jalankan: python -m pip install -r backend\\requirements.txt di virtual environment yang dipakai backend."
)
from backend.services.page_classifier import classify_page_source
from backend.services.pdfjs_native import (
    extract_grid_table,
    inspect_with_pdfjs,
    page_native_text,
)
from backend.services.table_extractor import extract_tables_from_page

logger = logging.getLogger(__name__)


def get_parser_status() -> dict[str, str | bool]:
    ready = ocr_dependencies_available()
    return {
        "ready": True,
        "engine": "PyMuPDF + RapidOCR (ONNX Runtime)",
        "ocr_ready": ready,
        "message": (
            "Native PDF extraction and scan-page OCR are available."
            if ready else
            "Native PDF extraction is available, but OCR dependencies are not installed in this environment."
        ),
    }


def _page_image_coverage(
    page: Any,
    image_count: int,
    image_info: list[dict[str, Any]] | None = None,
) -> float:
    """Calculate the union coverage of embedded image rectangles on a page."""
    page_rect = page.rect
    page_area = max(1.0, float(page_rect.width) * float(page_rect.height))
    if image_info is None:
        try:
            image_info = page.get_image_info(xrefs=True)
        except Exception:
            return 0.0

    # get_image_info can report the same pattern/shading bbox hundreds of
    # thousands of times. Exact duplicate rectangles do not change a union's
    # area, so keep one copy before the sweep. Do not round coordinates here:
    # near-equal placements may represent distinct geometry.
    rectangles: set[tuple[float, float, float, float]] = set()
    for item in image_info:
        bbox = item.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        try:
            rect = pymupdf.Rect(bbox) & page_rect
        except Exception:
            continue
        if rect.is_empty or rect.is_infinite or rect.width <= 0 or rect.height <= 0:
            continue
        rectangles.add((float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)))

    if not rectangles:
        return 0.0

    # Sweep vertical slabs and union their y-intervals, avoiding double-counting
    # overlapping image layers (e.g. MRC scan PDFs).
    x_edges = sorted({edge for rect in rectangles for edge in (rect[0], rect[2])})
    union_area = 0.0
    for x0, x1 in zip(x_edges, x_edges[1:]):
        if x1 <= x0:
            continue
        midpoint = (x0 + x1) / 2.0
        intervals = sorted(
            (rect[1], rect[3]) for rect in rectangles
            if rect[0] < midpoint < rect[2]
        )
        if not intervals:
            continue
        covered_y = 0.0
        current_start, current_end = intervals[0]
        for y0, y1 in intervals[1:]:
            if y0 <= current_end:
                current_end = max(current_end, y1)
            else:
                covered_y += current_end - current_start
                current_start, current_end = y0, y1
        covered_y += current_end - current_start
        union_area += (x1 - x0) * covered_y

    return max(0.0, min(1.0, union_area / page_area))


def _page_image_info_for_classification(page: Any) -> tuple[int, list[dict[str, Any]]]:
    """Avoid expanding vector patterns/shadings as hundreds of thousands of images.

    ``get_image_info`` reports painted pattern/shading instances as image-info
    entries even when the page has no embedded raster image. Resource inspection
    plus a scan for inline-image operators is cheap and sufficient to skip that
    expansion. Pages with image resources or inline images still use the
    existing bbox-based coverage path.
    """
    try:
        image_resources = page.get_images(full=True)
    except Exception:
        image_resources = None

    if image_resources is not None and not image_resources:
        inline_images = _page_contains_inline_image(page)
        if inline_images is False:
            return 0, []

    try:
        image_info = page.get_image_info(xrefs=False)
    except Exception:
        return (len(image_resources) if image_resources is not None else 0), []

    image_count = len(image_resources) if image_resources is not None else len(image_info)
    if image_resources is not None and not image_resources and image_info:
        # Inline images are not XObject resources, so include their actual
        # placements in the count when falling back to image-info inspection.
        image_count = len(image_info)
    return image_count, image_info


def _page_contains_inline_image(page: Any) -> bool | None:
    """Return whether a page content stream contains a PDF ``BI`` operator.

    ``None`` means the content could not be inspected; callers then use the
    established image-info path conservatively. A false positive only costs
    performance because it falls back to the old, coverage-based inspection.
    """
    try:
        document = page.parent
        for xref in page.get_contents() or []:
            stream = document.xref_stream(int(xref))
            if stream is None:
                return None
            if re.search(rb"(?<![A-Za-z0-9])BI(?![A-Za-z0-9])", stream):
                return True
        return False
    except Exception:
        return None


def _has_usable_table_headers(table: dict[str, Any]) -> bool:
    """Whether a table has enough plausible source-derived column labels."""
    headers = table.get("headers")
    if not isinstance(headers, list):
        return False
    cleaned = [str(value).strip() for value in headers]
    column_count = int(table.get("column_count") or len(cleaned) or 0)
    if column_count < 2 or len(cleaned) != column_count:
        return False
    nonempty = [value for value in cleaned if value]
    if len(nonempty) < 2:
        return False
    combined = " ".join(nonempty).casefold()
    if any(marker in combined for marker in ("@", "email", "e-mail", "phone", "telephone", "fax", "http://", "https://")):
        return False
    if len(combined) > 150:
        return False
    return True


def _inherit_continuation_table_headers(pages: list[dict[str, Any]]) -> None:
    """Reuse prior source headers on a headerless continuation table page.

    Native and OCR-derived continuation tables with the same number of columns
    are considered. Header labels are copied from the immediately preceding page;
    their bounding boxes are not copied because the header is not physically
    present on the continuation page.
    """
    for index in range(1, len(pages)):
        current_page = pages[index]
        previous_page = pages[index - 1]
        page_height = float(current_page.get("_page_height") or 0.0)
        if not page_height:
            continue

        for table in current_page.get("tables", []):
            if table.get("table_role") != "line_items":
                continue
            if _has_usable_table_headers(table):
                continue
            columns = int(table.get("column_count") or 0)
            if columns < 2:
                continue

            source_bbox = table.get("source_bbox")
            if isinstance(source_bbox, (list, tuple)) and len(source_bbox) == 4:
                # A continuation table should begin in the upper portion of the
                # next page, not in a footer or a separate later table region.
                if float(source_bbox[1]) > page_height * 0.40:
                    continue

            previous_candidates = [
                prior for prior in previous_page.get("tables", [])
                if prior.get("table_role") == "line_items"
                and int(prior.get("column_count") or 0) == columns
                and _has_usable_table_headers(prior)
            ]
            if not previous_candidates:
                continue

            source = previous_candidates[-1]
            headers = [str(value).strip() for value in source.get("headers", [])]
            table["headers"] = headers
            table["header_rows"] = [headers]
            table["header_cells"] = [
                {
                    "row": 0,
                    "column": column,
                    "text": label,
                    "rowspan": 1,
                    "colspan": 1,
                    "source_bbox": None,
                }
                for column, label in enumerate(headers)
            ]
            table["header_source"] = "inherited_from_previous_page"
            table["header_source_page"] = previous_page.get("page_number")
            table["header_detected"] = False
            table["header_inherited"] = True


def _empty_table_result(status: str, error: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "tables": [],
        "source_table_count": 0,
        "metadata_items": [],
        "document_metadata_table": None,
        "summary_items": [],
        "methods_attempted": [],
        "methods_failed": [],
    }
    if error:
        result["error"] = error
    return result


def inspect_pdf(
    pdf_bytes: bytes,
    *,
    run_ocr: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Inspect PDF pages; use native extraction for digital pages and OCR for scans.

    With run_ocr=False the digital pages are extracted immediately and scan pages
    are returned as ``ocr_pending`` without running OCR, so the UI can show the
    digital result first and request OCR separately.
    """
    if not pdf_bytes:
        raise ValueError("File PDF kosong.")

    def report_progress(
        phase: str,
        *,
        completed_pages: int = 0,
        total_pages: int | None = None,
        current_page: int | None = None,
        status: str = "processing",
        failed_pages: int = 0,
        progress_percent: int | None = None,
        work_units_completed: int | None = None,
        work_units_total: int | None = None,
        ocr_pages_processed: int | None = None,
        ocr_pages_total: int | None = None,
    ) -> None:
        if progress_callback is None:
            return
        try:
            payload = {
                "phase": phase,
                "status": status,
                "completed_pages": completed_pages,
                "total_pages": total_pages,
                "current_page": current_page,
                "failed_pages": failed_pages,
            }
            if progress_percent is not None:
                payload["progress_percent"] = max(0, min(100, int(progress_percent)))
            if work_units_completed is not None:
                payload["work_units_completed"] = max(0, int(work_units_completed))
            if work_units_total is not None:
                payload["work_units_total"] = max(0, int(work_units_total))
            if ocr_pages_processed is not None:
                payload["ocr_pages_processed"] = max(0, int(ocr_pages_processed))
            if ocr_pages_total is not None:
                payload["ocr_pages_total"] = max(0, int(ocr_pages_total))
            progress_callback(payload)
        except Exception:
            # Progress reporting is observational; its failures must not alter
            # PDF extraction or the returned table data.
            return

    report_progress("reading_pdf")
    document_cache_key: str | None = None
    pdfjs_result = inspect_with_pdfjs(pdf_bytes)
    pdfjs_pages = pdfjs_result[0] if pdfjs_result else None
    pdfjs_total_seconds = pdfjs_result[1] if pdfjs_result else 0.0

    ocr_executor: ThreadPoolExecutor | None = None
    page_contexts: list[dict[str, Any]] = []
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError("File tidak dapat dibaca sebagai PDF yang valid.") from exc

    try:
        if document.needs_pass:
            raise ValueError("PDF dilindungi kata sandi dan belum dapat dibaca.")
        if len(document) == 0:
            raise ValueError("PDF tidak memiliki halaman.")

        total_pages = len(document)
        report_progress("classifying_page", total_pages=total_pages)

        pages: list[dict[str, Any]] = []
        ocr_pages_attempted = 0
        ocr_pages_completed = 0
        ocr_pages_failed = 0
        ocr_pages_pending = 0
        total_native_tables = 0

        ocr_ready = run_ocr and ocr_dependencies_available()
        pending_ocr: list[int] = []
        ocr_inference_finished = 0
        ocr_inference_failed = 0
        completed_work_units = 0
        total_work_units = 0

        # Classify on this thread because PyMuPDF page objects are not sent to
        # workers. OCR render/preparation follows after page routing is known.
        for page_index, page in enumerate(document):
            page_started = time.perf_counter()
            report_progress(
                "classifying_page",
                completed_pages=0,
                total_pages=total_pages,
                current_page=page_index + 1,
            )
            stage_started = time.perf_counter()
            page_pdfjs = (
                pdfjs_pages[page_index]
                if pdfjs_pages is not None and page_index < len(pdfjs_pages)
                else None
            )
            if page_pdfjs is not None:
                native_text = page_native_text(page_pdfjs)
                native_text_seconds = pdfjs_total_seconds / max(1, len(pdfjs_pages))
            else:
                native_text = page.get_text("text", sort=True).strip()
                native_text_seconds = time.perf_counter() - stage_started
            has_text = bool(native_text)
            stage_started = time.perf_counter()
            # Use resource enumeration first: it avoids expanding PDF vector
            # patterns/shadings into massive repeated image-info entry lists.
            # Actual raster pages still use the unchanged bbox coverage logic.
            image_count, image_info = _page_image_info_for_classification(page)
            image_coverage = _page_image_coverage(page, image_count, image_info)
            image_info_coverage_seconds = time.perf_counter() - stage_started
            stage_started = time.perf_counter()
            source_candidate = classify_page_source(
                has_extractable_text=has_text,
                image_count=image_count,
                image_coverage=image_coverage,
            )
            classification_seconds = time.perf_counter() - stage_started

            use_ocr = (
                not has_text
                or source_candidate in {"scan_candidate", "scan_with_text_layer"}
            )

            context: dict[str, Any] = {
                "page": page, "page_started": page_started,
                "native_text": native_text,
                "native_text_seconds": native_text_seconds,
                "image_count": image_count,
                "image_coverage": image_coverage,
                "image_info_coverage_seconds": image_info_coverage_seconds,
                "classification_seconds": classification_seconds,
                "source_candidate": source_candidate, "use_ocr": use_ocr,
                "pdfjs_page": page_pdfjs,
                "ocr_prepared": None, "ocr_future": None,
                "ocr_result": None, "ocr_prepare_error": None,
            }
            page_contexts.append(context)

        ocr_contexts = [
            (index, context) for index, context in enumerate(page_contexts)
            if context["use_ocr"]
        ]
        counted_ocr_pages = len(ocr_contexts) if run_ocr else 0
        total_work_units = total_pages * 2 + counted_ocr_pages + 1
        completed_work_units = total_pages  # page classification is complete

        def current_progress_percent() -> int:
            if total_work_units <= 0:
                return 0
            return min(99, round(completed_work_units * 100 / total_work_units))

        report_progress(
            "preparing_ocr" if ocr_contexts and run_ocr else "reconstructing_tables",
            total_pages=total_pages,
            progress_percent=current_progress_percent(),
            work_units_completed=completed_work_units,
            work_units_total=total_work_units,
            ocr_pages_processed=ocr_inference_finished,
            ocr_pages_total=counted_ocr_pages,
        )

        def collect_ocr_result(page_index: int) -> None:
            nonlocal ocr_inference_finished, ocr_inference_failed, completed_work_units
            context = page_contexts[page_index]
            report_progress(
                "ocr_inference",
                total_pages=total_pages,
                current_page=page_index + 1,
                progress_percent=current_progress_percent(),
                work_units_completed=completed_work_units,
                work_units_total=total_work_units,
                ocr_pages_processed=ocr_inference_finished,
                ocr_pages_total=counted_ocr_pages,
                failed_pages=ocr_inference_failed,
            )
            try:
                context["ocr_result"] = context["ocr_future"].result()
            except Exception as exc:
                context["ocr_prepare_error"] = str(exc)[:500]
                ocr_inference_failed += 1
            finally:
                discard_prepared_ocr(context.get("ocr_prepared"))
                context["ocr_future"] = None
            ocr_inference_finished += 1
            completed_work_units += 1
            report_progress(
                "ocr_inference",
                total_pages=total_pages,
                current_page=page_index + 1,
                progress_percent=current_progress_percent(),
                work_units_completed=completed_work_units,
                work_units_total=total_work_units,
                ocr_pages_processed=ocr_inference_finished,
                ocr_pages_total=counted_ocr_pages,
                failed_pages=ocr_inference_failed,
            )

        if ocr_contexts and run_ocr and not ocr_ready:
            # Count each page's failed OCR attempt as completed work. The page
            # result below carries the explicit OCR-unavailable error state.
            ocr_inference_finished = counted_ocr_pages
            completed_work_units += counted_ocr_pages
            report_progress(
                "ocr_inference",
                total_pages=total_pages,
                progress_percent=current_progress_percent(),
                work_units_completed=completed_work_units,
                work_units_total=total_work_units,
                ocr_pages_processed=ocr_inference_finished,
                ocr_pages_total=counted_ocr_pages,
                failed_pages=counted_ocr_pages,
            )
        elif ocr_contexts and ocr_ready:
            ocr_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="docuflow-page-ocr"
            )
            try:
                for page_index, context in ocr_contexts:
                    try:
                        if document_cache_key is None:
                            document_cache_key = hashlib.sha256(pdf_bytes).hexdigest()
                        prepared = prepare_ocr_page(
                            context["page"], dpi=150, cache_key=document_cache_key,
                            page_number=page_index + 1,
                        )
                        context["ocr_prepared"] = prepared
                        context["ocr_future"] = ocr_executor.submit(
                            infer_prepared_ocr, prepared
                        )
                        pending_ocr.append(page_index)
                        if len(pending_ocr) > 2:
                            collect_ocr_result(pending_ocr.pop(0))
                    except Exception as exc:
                        context["ocr_prepare_error"] = str(exc)[:500]
                        ocr_inference_failed += 1
                        completed_work_units += 1
                        ocr_inference_finished += 1
                        report_progress(
                            "ocr_inference",
                            total_pages=total_pages,
                            current_page=page_index + 1,
                            progress_percent=current_progress_percent(),
                            work_units_completed=completed_work_units,
                            work_units_total=total_work_units,
                            ocr_pages_processed=ocr_inference_finished,
                            ocr_pages_total=counted_ocr_pages,
                            failed_pages=ocr_inference_failed,
                        )
                for pending_index in pending_ocr:
                    collect_ocr_result(pending_index)
            finally:
                ocr_executor.shutdown(wait=True)
                ocr_executor = None

        report_progress(
            "reconstructing_tables",
            total_pages=total_pages,
            progress_percent=current_progress_percent(),
            work_units_completed=completed_work_units,
            work_units_total=total_work_units,
            ocr_pages_processed=ocr_inference_finished,
            ocr_pages_total=counted_ocr_pages,
        )

        for page_index, context in enumerate(page_contexts):
            page = context["page"]
            page_started = context["page_started"]
            native_text = context["native_text"]
            native_text_seconds = context["native_text_seconds"]
            image_count = context["image_count"]
            image_coverage = context["image_coverage"]
            image_info_coverage_seconds = context["image_info_coverage_seconds"]
            classification_seconds = context["classification_seconds"]
            source_candidate = context["source_candidate"]
            use_ocr = context["use_ocr"]
            ocr_applied = False
            ocr_word_count = 0
            ocr_elapsed_seconds: float | None = None
            ocr_inference_timing: float | None = None
            render_elapsed_seconds: float | None = None
            ocr_cache_hit = False
            deskew_angle_degrees = 0.0
            table_timings: dict[str, float] = {}
            ocr_error: str | None = None

            if use_ocr and not run_ocr:
                ocr_pages_pending += 1
                table_result = _empty_table_result("ocr_pending")
            elif use_ocr and not ocr_dependencies_available():
                ocr_pages_attempted += 1
                ocr_pages_failed += 1
                ocr_error = OCR_NOT_INSTALLED_MESSAGE
                table_result = _empty_table_result("ocr_failed", ocr_error)
            elif use_ocr:
                ocr_pages_attempted += 1
                try:
                    if context["ocr_prepare_error"]:
                        raise RuntimeError(context["ocr_prepare_error"])
                    prepared = context["ocr_prepared"]
                    ocr_result = context["ocr_result"]
                    if prepared is None or ocr_result is None:
                        raise RuntimeError("OCR halaman tidak menghasilkan hasil inferensi.")
                    ocr_page = build_ocr_page_proxy(page, prepared, ocr_result)
                    render_elapsed_seconds = getattr(ocr_page, "render_elapsed_seconds", 0.0)
                    ocr_cache_hit = bool(getattr(ocr_page, "ocr_cache_hit", False))
                    deskew_angle_degrees = float(getattr(ocr_page, "deskew_angle_degrees", 0.0))
                    table_result = extract_tables_from_page(ocr_page, timing_metrics=table_timings)
                    ocr_applied = True
                    ocr_pages_completed += 1
                    ocr_word_count = ocr_page.ocr_word_count
                    ocr_elapsed_seconds = ocr_page.ocr_elapsed_seconds
                    ocr_inference_timing = getattr(
                        ocr_page, "ocr_inference_elapsed_seconds", ocr_elapsed_seconds
                    )
                except Exception as exc:
                    ocr_pages_failed += 1
                    ocr_error = str(exc)[:500]
                    table_result = _empty_table_result("ocr_failed", ocr_error)
                finally:
                    discard_prepared_ocr(context.get("ocr_prepared"))
            else:
                fast_table = (
                    extract_grid_table(context["pdfjs_page"])
                    if context.get("pdfjs_page") is not None else None
                )
                if fast_table is not None:
                    fast_table["source_page"] = page_index + 1
                    table_result = {
                        "metadata_items": [],
                        "document_metadata_table": None,
                        "summary_items": [],
                        "status": "table_found",
                        "tables": [fast_table],
                        "source_table_count": 1,
                        "methods_attempted": ["pdfjs_source_grid"],
                        "methods_failed": [],
                    }
                else:
                    table_result = extract_tables_from_page(page, timing_metrics=table_timings)
                total_native_tables += int(
                    table_result.get("source_table_count", len(table_result.get("tables", [])))
                )

            route = "scan" if use_ocr else "native"
            logger.info(
                "PDF page timing page=%d route=%s classification=%.6f native_text=%.6f "
                "image_info_coverage=%.6f native_table_detection=%.6f "
                "render=%.6f ocr_inference=%.6f table_reconstruction=%.6f "
                "validation=%.6f total=%.6f",
                page_index + 1, route, classification_seconds, native_text_seconds,
                image_info_coverage_seconds,
                table_timings.get("native_table_detection_seconds", 0.0),
                render_elapsed_seconds or 0.0, ocr_inference_timing or 0.0,
                table_timings.get("table_reconstruction_seconds", 0.0),
                table_timings.get("validation_seconds", 0.0),
                time.perf_counter() - page_started,
            )

            pages.append({
                "page_number": page_index + 1,
                "_page_height": float(page.rect.height),
                "source_candidate": source_candidate,
                "image_coverage": round(image_coverage, 4),
                # ``has_text`` is assigned while classifying all pages above;
                # by this result-building loop it contains the last page's
                # value. Derive the flag from this page's own native text so
                # mixed/native pages are not misreported as scans.
                "has_extractable_text": bool(native_text),
                "native_text": native_text,
                "native_text_trusted": not use_ocr,
                "native_text_char_count": len(native_text),
                "image_count": image_count,
                "ocr_applied": ocr_applied,
                "ocr_word_count": ocr_word_count,
                "ocr_elapsed_seconds": ocr_elapsed_seconds,
                "ocr_cache_hit": ocr_cache_hit,
                "deskew_angle_degrees": deskew_angle_degrees,
                "ocr_error": ocr_error,
                "table_extraction_status": table_result.get("status", "detection_error"),
                "tables": table_result.get("tables", []),
                "source_table_count": int(table_result.get("source_table_count", len(table_result.get("tables", [])))),
                "metadata_items": table_result.get("metadata_items", []),
                "document_metadata_table": table_result.get("document_metadata_table"),
                "summary_items": table_result.get("summary_items", []),
                "table_detection_methods": table_result.get("methods_attempted", []),
                "table_detection_methods_failed": table_result.get("methods_failed", []),
            })

            completed_work_units += 1
            report_progress(
                "extracting_page",
                completed_pages=page_index + 1,
                total_pages=total_pages,
                current_page=page_index + 1,
                failed_pages=sum(
                    page_result.get("table_extraction_status") in {
                        "ocr_failed", "detection_error", "extraction_error",
                    }
                    for page_result in pages
                ),
                progress_percent=current_progress_percent(),
                work_units_completed=completed_work_units,
                work_units_total=total_work_units,
                ocr_pages_processed=ocr_inference_finished,
                ocr_pages_total=counted_ocr_pages,
            )

        report_progress(
            "validating_tables",
            completed_pages=len(pages),
            total_pages=total_pages,
            failed_pages=sum(
                page_result.get("table_extraction_status") in {
                    "ocr_failed", "detection_error", "extraction_error",
                }
                for page_result in pages
            ),
            progress_percent=current_progress_percent(),
            work_units_completed=completed_work_units,
            work_units_total=total_work_units,
            ocr_pages_processed=ocr_inference_finished,
            ocr_pages_total=counted_ocr_pages,
        )
        _inherit_continuation_table_headers(pages)
        for page in pages:
            page.pop("_page_height", None)

        total_tables = sum(int(page.get("source_table_count", len(page["tables"]))) for page in pages)
        total_summary_items = sum(len(page["summary_items"]) for page in pages)
        ocr_status = (
            "partial_failure" if ocr_pages_failed else
            "pending" if ocr_pages_pending else
            "completed" if ocr_pages_completed else
            "ready_no_scan_pages"
        )
        overall_status = (
            "partial_failure" if ocr_pages_failed else
            "ocr_pending" if ocr_pages_pending else
            "completed"
        )

        result = {
            "page_count": len(document),
            "pages": pages,
            # Preserve the old field for existing frontend callers; add an
            # accurately named total that includes OCR-derived source tables.
            "total_native_tables_detected": total_native_tables,
            "total_tables_detected": total_tables,
            "total_summary_items_detected": total_summary_items,
            "scanned_pages_waiting_for_ocr": ocr_pages_pending,
            "scanned_pages_ocr_failed": ocr_pages_failed,
            "ocr_pages_attempted": ocr_pages_attempted,
            "ocr_pages_completed": ocr_pages_completed,
            "ocr_status": ocr_status,
            "table_extraction_status": overall_status,
        }
        page_failures = sum(
            page.get("table_extraction_status") in {
                "ocr_failed", "detection_error", "extraction_error",
            }
            for page in pages
        )
        report_progress(
            "completed" if page_failures == 0 else "failed",
            status="completed" if page_failures == 0 else "partial_failure",
            completed_pages=len(pages),
            total_pages=len(pages),
            failed_pages=page_failures,
            progress_percent=100,
            work_units_completed=total_work_units,
            work_units_total=total_work_units,
            ocr_pages_processed=ocr_inference_finished,
            ocr_pages_total=counted_ocr_pages,
        )
        return result
    finally:
        if ocr_executor is not None:
            ocr_executor.shutdown(wait=True, cancel_futures=True)
        for context in page_contexts:
            prepared = context.get("ocr_prepared")
            if prepared is not None:
                discard_prepared_ocr(prepared)
        document.close()
