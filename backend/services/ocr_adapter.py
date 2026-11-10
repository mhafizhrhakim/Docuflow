"""RapidOCR adapter exposing OCR word boxes through a PyMuPDF-like page API."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import math
import tempfile
import threading
import time
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

import pymupdf

from backend.core.config import settings

_ENGINE: Any | None = None
_ENGINE_LOCK = threading.Lock()
# Share the one ONNX model across at most two simultaneous page inferences.
# A bounded same-engine probe confirmed identical word boxes/text and better
# two-page throughput on the current CPU; this avoids duplicating model memory.
_OCR_INFERENCE_SEMAPHORE = threading.BoundedSemaphore(2)
# If model loading fails (for example the first-run model download is blocked),
# remember it briefly so every page/upload does not retry the slow download.
_ENGINE_FAILURE: tuple[float, str] | None = None
_FAILURE_RETRY_SECONDS = 120.0
_WARMUP: dict[str, Any] = {"state": "idle", "error": None, "seconds": None}
_WARMUP_LOCK = threading.Lock()
_OCR_CACHE_DIR = Path(__file__).resolve().parents[1] / "storage" / "work" / "ocr-cache"
_OCR_CACHE_VERSION = 2
_OCR_CACHE_MAX_BYTES = 256 * 1024 * 1024
_OCR_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_OCR_CACHE_LOCK = threading.Lock()


def _ocr_cache_path(document_key: str, page_number: int, dpi: int) -> Path:
    identity = f"{_OCR_CACHE_VERSION}|{document_key}|{page_number}|{dpi}|ppocrv5-mobile-en"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _OCR_CACHE_DIR / f"docuflow-ocr-cache-{digest}.json"


def _read_ocr_cache(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            return None
        with path.open("r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
        entries = payload.get("entries")
        width, height = int(payload["image_width"]), int(payload["image_height"])
        if not isinstance(entries, list) or width <= 0 or height <= 0:
            return None
        if time.time() - float(payload.get("created_at", 0)) > _OCR_CACHE_MAX_AGE_SECONDS:
            path.unlink(missing_ok=True)
            return None
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
                return None
            points = entry.get("points")
            if not isinstance(points, list) or len(points) < 4:
                return None
        stamp_regions = payload.get("stamp_regions")
        if stamp_regions is not None and (
            not isinstance(stamp_regions, list)
            or any(
                not isinstance(box, list)
                or len(box) != 4
                or any(
                    not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not 0.0 <= float(value) <= 1.0
                    for value in box
                )
                for box in stamp_regions
            )
        ):
            payload.pop("stamp_regions", None)
        return payload
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_ocr_cache(
    path: Path,
    entries: list[dict[str, Any]],
    width: int,
    height: int,
    stamp_regions: list[list[float]] | None = None,
) -> None:
    """Write raw OCR output with bounded local retention."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _OCR_CACHE_VERSION,
            "created_at": time.time(),
            "image_width": width,
            "image_height": height,
            "entries": entries,
        }
        if stamp_regions is not None:
            payload["stamp_regions"] = stamp_regions
        with _OCR_CACHE_LOCK:
            # A partial/truncated JSON file is treated as a cache miss by the
            # reader, so direct writes remain safe even on Windows filesystems
            # where atomic replace across sandboxed temp paths is unavailable.
            with path.open("w", encoding="utf-8") as cache_file:
                json.dump(payload, cache_file, ensure_ascii=False, separators=(",", ":"))

            now = time.time()
            cache_files = sorted(
                path.parent.glob("docuflow-ocr-cache-*.json"),
                key=lambda item: item.stat().st_mtime,
            )
            total_bytes = 0
            retained: list[Path] = []
            for cache_file in cache_files:
                try:
                    age = now - cache_file.stat().st_mtime
                    size = cache_file.stat().st_size
                    if age > _OCR_CACHE_MAX_AGE_SECONDS:
                        cache_file.unlink(missing_ok=True)
                        continue
                    retained.append(cache_file)
                    total_bytes += size
                except OSError:
                    continue
            for cache_file in retained:
                if total_bytes <= _OCR_CACHE_MAX_BYTES:
                    break
                try:
                    total_bytes -= cache_file.stat().st_size
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        # Caching is an optimization; storage failures must never fail extraction.
        return


def _deskew_image(image: Any) -> tuple[Any, Any | None, float]:
    """Correct only strongly supported page skew in the 0.5–3 degree range."""
    try:
        import cv2
        import numpy as np

        if image is None or len(image.shape) < 2:
            return image, None, 0.0
        height, width = image.shape[:2]
        if width < 240 or height < 240:
            return image, None, 0.0
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        segments = cv2.HoughLinesP(
            edges, 1, np.pi / 1800, threshold=max(24, int(width * 0.025)),
            minLineLength=max(90, int(width * 0.16)),
            maxLineGap=max(12, int(width * 0.025)),
        )
        if segments is None:
            return image, None, 0.0
        angles: list[float] = []
        for segment in segments.reshape(-1, 4):
            x0, y0, x1, y1 = (float(value) for value in segment)
            dx, dy = x1 - x0, y1 - y0
            length = math.hypot(dx, dy)
            angle = math.degrees(math.atan2(dy, dx))
            if length >= width * 0.16 and abs(angle) <= 3.0:
                angles.append(angle)
        if len(angles) < 3:
            return image, None, 0.0
        angle = float(median(angles))
        aligned = [value for value in angles if abs(value - angle) <= 0.35]
        if len(aligned) < 3 or not 0.5 <= abs(angle) <= 3.0:
            return image, None, 0.0

        matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
        rotated = cv2.warpAffine(
            image, matrix, (width, height), flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
        )
        return rotated, matrix, angle
    except Exception:
        # Missing/unsupported preprocessing must leave the existing OCR path intact.
        return image, None, 0.0


def _map_entries_to_original_image(entries: list[dict[str, Any]], matrix: Any) -> None:
    """Map OCR polygons from the deskewed raster back to source-page pixels."""
    try:
        import cv2
        import numpy as np

        inverse = cv2.invertAffineTransform(matrix)
        for entry in entries:
            points = np.asarray(entry["points"], dtype="float64")
            homogeneous = np.column_stack((points, np.ones(len(points))))
            mapped = homogeneous @ inverse.T
            entry["points"] = [[float(x), float(y)] for x, y in mapped]
    except Exception:
        # A failed coordinate transform must not silently leave boxes in a
        # different coordinate system; mark it as unusable so OCR fails clearly.
        entries.clear()


def ocr_dependencies_available() -> bool:
    """Return whether the runtime dependencies for local CPU OCR are importable."""
    return (
        importlib.util.find_spec("rapidocr") is not None
        and importlib.util.find_spec("onnxruntime") is not None
    )


def _get_engine() -> Any:
    global _ENGINE, _ENGINE_FAILURE
    if _ENGINE is None:
        failure = _ENGINE_FAILURE
        if failure and time.monotonic() - failure[0] < _FAILURE_RETRY_SECONDS:
            raise RuntimeError(failure[1])
        with _ENGINE_LOCK:
            if _ENGINE is None:
                try:
                    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

                    _ENGINE = RapidOCR(
                        params={
                            "Det.engine_type": EngineType.ONNXRUNTIME,
                            "Det.lang_type": "multi",
                            "Det.model_type": ModelType.MOBILE,
                            "Det.ocr_version": OCRVersion.PPOCRV5,
                            "Cls.engine_type": EngineType.ONNXRUNTIME,
                            "Cls.lang_type": "multi",
                            "Cls.model_type": ModelType.MOBILE,
                            "Cls.ocr_version": OCRVersion.PPOCRV5,
                            "Rec.engine_type": EngineType.ONNXRUNTIME,
                            "Rec.lang_type": "en",
                            "Rec.model_type": ModelType.MOBILE,
                            "Rec.ocr_version": OCRVersion.PPOCRV5,
                            "Global.return_word_box": True,
                            "EngineConfig.onnxruntime.intra_op_num_threads": (
                                settings.ocr_intra_op_threads
                            ),
                            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                        }
                    )
                    _ENGINE_FAILURE = None
                except Exception as exc:
                    message = (
                        "Model OCR gagal dimuat: " + str(exc)[:300]
                        + " (model PP-OCRv5 diunduh otomatis saat pertama dipakai; "
                        "pastikan koneksi internet tersedia sekali, atau salin folder "
                        "rapidocr\\models dari environment yang sudah berhasil.)"
                    )
                    _ENGINE_FAILURE = (time.monotonic(), message)
                    raise RuntimeError(message) from exc
    return _ENGINE


def warmup_engine() -> dict[str, Any]:
    """Load all lazy ONNX sessions before reporting OCR as ready. Never raises."""
    with _WARMUP_LOCK:
        if _WARMUP["state"] == "ready":
            return dict(_WARMUP)
        start = time.perf_counter()
        _WARMUP.update(state="loading", error=None)
        try:
            engine = _get_engine()
            # RapidOCR constructs its facade eagerly but defers each ONNX
            # InferenceSession until the first detected text crop. Load the
            # pinned 3.10.0 sessions now so the first user upload does not pay
            # model-session initialization time.
            for loader_name in ("_load_det_model", "_load_cls_model", "_load_rec_model"):
                loader = getattr(engine, loader_name)
                loader()
            _WARMUP.update(
                state="ready", error=None,
                seconds=round(time.perf_counter() - start, 2),
            )
        except Exception as exc:  # noqa: BLE001 - reported through health endpoint
            _WARMUP.update(state="failed", error=str(exc)[:500])
        return dict(_WARMUP)


def get_ocr_runtime_status() -> dict[str, Any]:
    """Small, cheap status summary for the health endpoint."""
    return {
        "dependencies_installed": ocr_dependencies_available(),
        "engine_state": "ready" if _ENGINE is not None else _WARMUP["state"],
        "load_seconds": _WARMUP["seconds"],
        "error": _WARMUP["error"],
    }


def _plain(value: Any) -> Any:
    return value.tolist() if hasattr(value, "tolist") else value


def _entries_from_result(result: Any) -> list[dict[str, Any]]:
    word_results = getattr(result, "word_results", None)
    entries: list[dict[str, Any]] = []
    for line_words in word_results or []:
        for item in line_words or []:
            try:
                text, confidence, box = item
                box = _plain(box)
                raw_text = str(text)
                if not raw_text.strip() or box is None or len(box) < 4:
                    continue
                points = [(float(p[0]), float(p[1])) for p in box]
                entries.append({
                    # Keep the recognizer's exact string alongside the trimmed
                    # version used by the existing geometry/table pipeline.
                    "text": raw_text.strip(),
                    "raw_text": raw_text,
                    "confidence": float(confidence),
                    "points": points,
                })
            except (TypeError, ValueError, IndexError):
                continue
    return entries


def _detect_colored_stamp_regions(image_bytes: bytes | None) -> list[list[float]]:
    """Find conservative, roughly round colored-ink regions in a page raster.

    The result is only a review hint. It never masks pixels or changes OCR text.
    Normalized boxes keep the cache independent from raster dimensions.
    """
    if not image_bytes:
        return []
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or len(image.shape) < 2:
            return []
        height, width = image.shape[:2]
        if width < 80 or height < 80:
            return []
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        # Saturated ink is distinct from typical black table text and gray rules.
        mask = cv2.inRange(
            hsv,
            np.array([0, 35, 45], dtype=np.uint8),
            np.array([179, 255, 255], dtype=np.uint8),
        )
        kernel_size = max(5, int(min(width, height) * 0.007) | 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        connected = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        page_area = float(width * height)
        min_side = max(28, int(min(width, height) * 0.035))
        max_side = int(min(width, height) * 0.35)
        regions: list[list[float]] = []
        for contour in contours:
            x, y, box_width, box_height = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            ratio = box_width / max(1.0, float(box_height))
            extent = area / max(1.0, float(box_width * box_height))
            if (
                min(box_width, box_height) < min_side
                or max(box_width, box_height) > max_side
                or area / page_area < 0.0008
                or not 0.68 <= ratio <= 1.48
                or extent < 0.38
            ):
                continue
            regions.append([
                round(x / width, 6),
                round(y / height, 6),
                round((x + box_width) / width, 6),
                round((y + box_height) / height, 6),
            ])
        return regions
    except Exception:
        # Optional visual analysis must never interrupt OCR extraction.
        return []


def recognize_stamp_occluded_numeric_crop(
    page: Any, bbox: tuple[float, float, float, float] | list[float],
) -> dict[str, Any] | None:
    """Re-read one small numeric cell after suppressing colored ink and rules.

    This is an opt-in recovery probe for cells already identified as overlapping
    a colored stamp. It renders only the supplied cell crop at high resolution,
    uses the existing RapidOCR recognizer, and never changes the page OCR cache.
    """
    source_page = getattr(page, "_page", page)
    if not callable(getattr(source_page, "get_pixmap", None)):
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
        if x1 <= x0 or y1 <= y0 or x1 - x0 > 160.0 or y1 - y0 > 45.0:
            return None
        import cv2
        import numpy as np

        scale = 600.0 / 72.0
        pixmap = source_page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=pymupdf.Rect(x0, y0, x1, y1),
            alpha=False,
        )
        if pixmap.width < 20 or pixmap.height < 12:
            return None
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n
        )[:, :, :3].copy()
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
        saturated = (hsv[:, :, 1] >= 35) & (hsv[:, :, 2] >= 65)
        if not np.any(saturated):
            return None
        hue_histogram = np.bincount(hsv[:, :, 0][saturated], minlength=180)
        dominant_hue = int(np.argmax(hue_histogram))
        hue_distance = np.abs(hsv[:, :, 0].astype(np.int16) - dominant_hue)
        hue_distance = np.minimum(hue_distance, 180 - hue_distance)
        colored_ink = saturated & (hue_distance <= 18)
        engine = _get_engine()
        recognizer = getattr(engine, "recognize_txt", None)
        if not callable(recognizer):
            return None

        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        max_channel = np.max(image, axis=2)
        variant_sources = []
        white_stamp = image.copy()
        white_stamp[colored_ink] = 255
        variant_sources.append(white_stamp)
        if np.any(colored_ink):
            max_channel_stamp = image.copy()
            max_channel_stamp[colored_ink] = np.repeat(
                max_channel[colored_ink, None], 3, axis=1
            )
            variant_sources.append(max_channel_stamp)
            luminance_stamp = image.copy()
            luminance_stamp[colored_ink] = np.repeat(
                gray[colored_ink, None], 3, axis=1
            )
            variant_sources.append(luminance_stamp)

        variant_readings: list[dict[str, Any]] = []
        for variant_source in variant_sources:
            variant_gray = cv2.cvtColor(variant_source, cv2.COLOR_RGB2GRAY)
            ink = cv2.threshold(
                variant_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
            )[1]
            horizontal_kernel = cv2.getStructuringElement(
                cv2.MORPH_RECT, (max(15, int(ink.shape[1] * 0.35)), 1)
            )
            horizontal_rules = cv2.morphologyEx(ink, cv2.MORPH_OPEN, horizontal_kernel)
            cleaned = cv2.subtract(ink, horizontal_rules)
            recognition_image = cv2.cvtColor(255 - cleaned, cv2.COLOR_GRAY2RGB)
            with _OCR_INFERENCE_SEMAPHORE:
                output = recognizer([recognition_image])
            texts = list(getattr(output, "txts", None) or [])
            scores = list(getattr(output, "scores", None) or [])
            if len(texts) != 1 or not str(texts[0]).strip() or not scores:
                continue
            variant_readings.append({
                "text": str(texts[0]).strip(),
                "confidence": max(0.0, min(1.0, float(scores[0]))),
            })
            if variant_readings[-1]["confidence"] >= 0.90:
                break

        if not variant_readings:
            return None
        best_reading = max(variant_readings, key=lambda item: item["confidence"])
        return {
            **best_reading,
            "variant_readings": variant_readings,
        }
    except Exception:
        # A failed optional crop probe must leave the normal review fallback intact.
        return None


def recognize_ocr_text_line_crop(
    page: Any,
    bbox: tuple[float, float, float, float] | list[float],
    *,
    dpi: int = 450,
) -> dict[str, Any] | None:
    """Read one geometrically isolated text line omitted by full-page OCR.

    This crop path is intended for rare row-baseline gaps found during table
    validation. It uses the existing RapidOCR recognizer and does not change or
    repopulate the page OCR cache.
    """
    source_page = getattr(page, "_page", page)
    if not callable(getattr(source_page, "get_pixmap", None)):
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
        width, height = x1 - x0, y1 - y0
        if (
            width < 20.0 or height < 5.0 or width > 800.0 or height > 36.0
            or dpi < 150 or dpi > 600
        ):
            return None
        import cv2
        import numpy as np

        scale = float(dpi) / 72.0
        pixmap = source_page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=pymupdf.Rect(x0, y0, x1, y1),
            alpha=False,
        )
        if pixmap.width < 30 or pixmap.height < 12:
            return None
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n
        )[:, :, :3].copy()
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        ink = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )[1]
        horizontal_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (max(15, int(ink.shape[1] * 0.35)), 1)
        )
        horizontal_rules = cv2.morphologyEx(ink, cv2.MORPH_OPEN, horizontal_kernel)
        cleaned = cv2.subtract(ink, horizontal_rules)
        recognition_image = cv2.cvtColor(255 - cleaned, cv2.COLOR_GRAY2RGB)
        engine = _get_engine()
        recognizer = getattr(engine, "recognize_txt", None)
        if not callable(recognizer):
            return None
        with _OCR_INFERENCE_SEMAPHORE:
            output = recognizer([recognition_image])
        texts = list(getattr(output, "txts", None) or [])
        scores = list(getattr(output, "scores", None) or [])
        if len(texts) != 1 or not str(texts[0]).strip() or not scores:
            return None
        return {
            "text": str(texts[0]).strip(),
            "confidence": max(0.0, min(1.0, float(scores[0]))),
            "crop_bbox": [x0, y0, x1, y1],
        }
    except Exception:
        # A failed localized read must never interrupt normal table output.
        return None


def _estimate_row_slope(entries: list[dict[str, Any]], image_width: int) -> float:
    """Estimate page skew from repeatedly aligned OCR word baselines.

    Returns dy/dx in pixel coordinates. A robust support score favours a slope
    that places several words on a common baseline. If evidence is weak, no
    correction is applied instead of guessing a slope.
    """
    centers: list[tuple[float, float]] = []
    for entry in entries:
        pts = entry["points"]
        centers.append((median([p[0] for p in pts]), median([p[1] for p in pts])))
    if len(centers) < 8:
        return 0.0

    bins: dict[int, int] = {}
    min_dx = max(120.0, image_width * 0.28)
    for i, (x1, y1) in enumerate(centers):
        for x2, y2 in centers[i + 1:]:
            dx = x2 - x1
            if dx < min_dx:
                continue
            slope = (y2 - y1) / dx
            if not math.isfinite(slope) or abs(slope) > 0.12:
                continue
            # 0.002 increments absorb a little OCR-box jitter while preserving
            # meaningful differences between skew angles.
            key = round(slope / 0.002)
            bins[key] = bins.get(key, 0) + 1

    if not bins:
        return 0.0

    # Inspect only the most frequently observed pairwise slope candidates.
    candidates = sorted(bins, key=bins.get, reverse=True)[:20]
    best_slope = 0.0
    best_support = 0
    window = max(10.0, image_width * 0.009)
    for key in candidates:
        slope = key * 0.002
        intercepts = sorted(y - slope * x for x, y in centers)
        left = 0
        support = 0
        for right, value in enumerate(intercepts):
            while value - intercepts[left] > window:
                left += 1
            support = max(support, right - left + 1)
        if support > best_support or (
            support == best_support and abs(slope) < abs(best_slope)
        ):
            best_support = support
            best_slope = slope

    # At least six words should support the correction; avoid correcting pages
    # where the observed geometry is too sparse or noisy.
    if best_support < 6 or abs(best_slope) < 0.008:
        return 0.0
    return max(-0.12, min(0.12, best_slope))


class OCRPageProxy:
    """Expose OCR text/bboxes using the subset of PyMuPDF page methods we need."""

    def __init__(
        self,
        original_page: Any,
        entries: list[dict[str, Any]],
        image_width: int,
        image_height: int,
        elapsed_seconds: float,
        render_elapsed_seconds: float = 0.0,
        cache_hit: bool = False,
        table_image_bytes: bytes | None = None,
        dpi: int = 150,
        stamp_regions: list[list[float]] | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self._page = original_page
        self.rect = original_page.rect
        self.ocr_inference_elapsed_seconds = float(elapsed_seconds)
        self.ocr_elapsed_seconds = round(float(elapsed_seconds), 3)
        self.render_elapsed_seconds = round(float(render_elapsed_seconds), 6)
        self.ocr_cache_hit = bool(cache_hit)
        self._table_image_bytes = table_image_bytes
        self._stamp_regions_normalized = stamp_regions
        self._cache_path = cache_path
        self._image_width = int(image_width)
        self._image_height = int(image_height)
        self._dpi = int(dpi)
        self._ocr_entries = entries
        self.deskew_angle_degrees = 0.0
        self.ocr_word_count = len(entries)
        self.skew_slope = _estimate_row_slope(entries, image_width)
        sx = float(self.rect.width) / max(1, image_width)
        sy = float(self.rect.height) / max(1, image_height)
        self.items: list[dict[str, Any]] = []

        for entry in entries:
            points = entry["points"]
            # If dy/dx is negative, subtracting slope*x corrects a line that
            # visually slopes upwards to the right (as in the tested scan).
            corrected = [(x, y - self.skew_slope * x) for x, y in points]
            x0 = min(point[0] for point in corrected) * sx
            y0 = min(point[1] for point in corrected) * sy
            x1 = max(point[0] for point in corrected) * sx
            y1 = max(point[1] for point in corrected) * sy
            self.items.append({
                "text": entry["text"],
                "raw_text": entry.get("raw_text", entry["text"]),
                "x0": max(float(self.rect.x0), x0),
                "y0": max(float(self.rect.y0), y0),
                "x1": min(float(self.rect.x1), x1),
                "y1": min(float(self.rect.y1), y1),
                "confidence": entry["confidence"],
            })

        self.items = [
            item for item in self.items
            if item["x1"] > item["x0"] and item["y1"] > item["y0"]
        ]
        self.items.sort(key=lambda item: (
            (item["y0"] + item["y1"]) / 2.0, item["x0"]
        ))

    def find_tables(self, *args: Any, **kwargs: Any) -> Any:
        # Do not let PyMuPDF's table text strategy use a potentially bad hidden
        # text layer on scanned pages. OCR geometry is the source of truth here.
        return SimpleNamespace(tables=[])

    def get_table_image_bytes(self) -> bytes | None:
        if self._table_image_bytes is None:
            try:
                scale = float(self._dpi) / 72.0
                pixmap = self._page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale), alpha=False
                )
                self._table_image_bytes = pixmap.tobytes("png")
                self._image_width, self._image_height = int(pixmap.width), int(pixmap.height)
            except Exception:
                return None
        return self._table_image_bytes

    def get_stamp_regions(self) -> list[list[float]]:
        """Return possible stamp bboxes in source-page points, computing lazily."""
        if self._stamp_regions_normalized is None:
            image_bytes = self.get_table_image_bytes()
            self._stamp_regions_normalized = _detect_colored_stamp_regions(image_bytes)
            if self._cache_path is not None:
                _write_ocr_cache(
                    self._cache_path,
                    self._ocr_entries,
                    self._image_width,
                    self._image_height,
                    self._stamp_regions_normalized,
                )
        return [
            [
                max(float(self.rect.x0), box[0] * float(self.rect.width)),
                max(float(self.rect.y0), box[1] * float(self.rect.height)),
                min(float(self.rect.x1), box[2] * float(self.rect.width)),
                min(float(self.rect.y1), box[3] * float(self.rect.height)),
            ]
            for box in self._stamp_regions_normalized
        ]

    def get_text(self, option: str = "text", *args: Any, **kwargs: Any) -> Any:
        if option == "words":
            return [
                (
                    item["x0"], item["y0"], item["x1"], item["y1"],
                    item["text"], 0, index, index,
                )
                for index, item in enumerate(self.items)
            ]

        if option == "dict":
            blocks = []
            for item in self.items:
                bbox = [item["x0"], item["y0"], item["x1"], item["y1"]]
                blocks.append({
                    "type": 0,
                    "bbox": bbox,
                    "lines": [{
                        "spans": [{
                            "text": item["text"],
                            "bbox": bbox,
                            "size": max(1.0, item["y1"] - item["y0"]),
                            "font": "OCR",
                            "flags": 0,
                            "color": 0,
                            "origin": [item["x0"], item["y1"]],
                        }]
                    }],
                })
            return {
                "width": self.rect.width,
                "height": self.rect.height,
                "blocks": blocks,
            }

        if option == "text":
            return "\n".join(item["text"] for item in self.items)

        return self._page.get_text(option, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)


def run_ocr_for_page(
    page: Any,
    dpi: int = 150,
    *,
    cache_key: str | None = None,
    page_number: int | None = None,
) -> OCRPageProxy:
    """Render one PDF page, OCR it using CPU ONNX Runtime, and return a proxy."""
    prepared = prepare_ocr_page(
        page, dpi=dpi, cache_key=cache_key, page_number=page_number
    )
    try:
        result = infer_prepared_ocr(prepared)
        return build_ocr_page_proxy(page, prepared, result)
    finally:
        discard_prepared_ocr(prepared)


def prepare_ocr_page(
    page: Any,
    dpi: int = 150,
    *,
    cache_key: str | None = None,
    page_number: int | None = None,
) -> dict[str, Any]:
    """Read cache or render/preprocess a page on the caller thread.

    PyMuPDF page rendering stays outside inference workers; only independent
    raster files are handed to the bounded ONNX inference pool.
    """
    cache_path = (
        _ocr_cache_path(cache_key, int(page_number), dpi)
        if cache_key is not None and page_number is not None else None
    )
    if cache_path is not None:
        cached = _read_ocr_cache(cache_path)
        if cached is not None:
            return {
                "cache_path": cache_path, "cached_entries": cached["entries"],
                "image_width": int(cached["image_width"]),
                "image_height": int(cached["image_height"]), "dpi": dpi,
                "render_elapsed": 0.0, "table_image_bytes": None,
                "stamp_regions": cached.get("stamp_regions"),
                "temp_path": None, "deskew_matrix": None, "deskew_angle": 0.0,
            }

    scale = float(dpi) / 72.0
    render_started = time.perf_counter()
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    image_width, image_height = int(pixmap.width), int(pixmap.height)
    table_image_bytes = pixmap.tobytes("png")

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="docuflow_ocr_", suffix=".png", delete=False) as file:
            temp_path = Path(file.name)
        # The same raster is sent to OCR by file path and retained for the
        # optional table-structure fallback. Write the already encoded PNG
        # bytes instead of asking MuPDF to encode the identical pixmap again.
        temp_path.write_bytes(table_image_bytes)
        deskew_matrix = None
        deskew_angle = 0.0
        try:
            import cv2
            image = cv2.imread(str(temp_path), cv2.IMREAD_COLOR)
            corrected_image, deskew_matrix, deskew_angle = _deskew_image(image)
            if deskew_matrix is not None:
                if not cv2.imwrite(str(temp_path), corrected_image):
                    deskew_matrix = None
                    deskew_angle = 0.0
        except Exception:
            # cv2 is optional at runtime; the dependency is declared for normal
            # installs, but OCR remains available if it cannot be imported.
            deskew_matrix = None
            deskew_angle = 0.0
        stamp_regions = _detect_colored_stamp_regions(table_image_bytes)
        render_elapsed = time.perf_counter() - render_started
        return {
            "cache_path": cache_path, "cached_entries": None,
            "image_width": image_width, "image_height": image_height,
            "dpi": dpi, "render_elapsed": render_elapsed,
            "table_image_bytes": table_image_bytes, "temp_path": temp_path,
            "stamp_regions": stamp_regions,
            "deskew_matrix": deskew_matrix, "deskew_angle": deskew_angle,
        }
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def infer_prepared_ocr(prepared: dict[str, Any]) -> dict[str, Any]:
    """Infer OCR on a prepared raster; safe to call in a bounded worker pool."""
    cached_entries = prepared.get("cached_entries")
    if cached_entries is not None:
        return {
            "entries": cached_entries,
            "image_width": prepared["image_width"],
            "image_height": prepared["image_height"],
            "elapsed": 0.0, "cache_hit": True,
        }

    # Load before entering the inference semaphore; engine creation uses the
    # initialization lock and must not deadlock with an inference slot.
    engine = _get_engine()
    start = time.perf_counter()
    with _OCR_INFERENCE_SEMAPHORE:
        cache_path = prepared.get("cache_path")
        cached = _read_ocr_cache(cache_path) if cache_path is not None else None
        if cached is not None:
            entries = cached["entries"]
            image_width = int(cached["image_width"])
            image_height = int(cached["image_height"])
            elapsed = 0.0
            cache_hit = True
        else:
            result = engine(str(prepared["temp_path"]), return_word_box=True)
            entries = _entries_from_result(result)
            if prepared.get("deskew_matrix") is not None:
                _map_entries_to_original_image(entries, prepared["deskew_matrix"])
            image_width = int(prepared["image_width"])
            image_height = int(prepared["image_height"])
            elapsed = time.perf_counter() - start
            cache_hit = False

    if not entries:
        raise RuntimeError("RapidOCR tidak menghasilkan kotak teks per kata.")
    if cache_path is not None and not cache_hit:
        _write_ocr_cache(
            cache_path,
            entries,
            image_width,
            image_height,
            prepared.get("stamp_regions"),
        )
    return {
        "entries": entries, "image_width": image_width,
        "image_height": image_height, "elapsed": elapsed,
        "cache_hit": cache_hit,
    }


def build_ocr_page_proxy(
    page: Any, prepared: dict[str, Any], result: dict[str, Any]
) -> OCRPageProxy:
    proxy = OCRPageProxy(
        page, result["entries"], result["image_width"], result["image_height"],
        result["elapsed"], prepared["render_elapsed"],
        cache_hit=result["cache_hit"],
        table_image_bytes=prepared.get("table_image_bytes"), dpi=prepared["dpi"],
        stamp_regions=prepared.get("stamp_regions"),
        cache_path=prepared.get("cache_path"),
    )
    proxy.deskew_angle_degrees = float(prepared.get("deskew_angle", 0.0))
    return proxy


def discard_prepared_ocr(prepared: dict[str, Any]) -> None:
    path = prepared.get("temp_path")
    if path is not None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
