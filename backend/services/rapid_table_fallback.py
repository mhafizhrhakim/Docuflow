"""Optional RapidTable structure fallback for weak OCR table geometry."""

from __future__ import annotations

from html.parser import HTMLParser
import logging
from pathlib import Path
import tempfile
import threading
from typing import Any

import pymupdf

logger = logging.getLogger(__name__)
_RAPID_TABLE_ENGINE: Any | None = None
_RAPID_TABLE_UNAVAILABLE = False
_RAPID_TABLE_LOCK = threading.Lock()


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, str]]] = []
        self._row: list[dict[str, str]] | None = None
        self._cell: dict[str, str] | None = None

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"kind": tag, "text": ""}

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _get_engine() -> Any:
    global _RAPID_TABLE_ENGINE, _RAPID_TABLE_UNAVAILABLE
    if _RAPID_TABLE_ENGINE is None:
        try:
            from rapid_table import ModelType, RapidTable, RapidTableInput
        except ModuleNotFoundError as exc:
            if exc.name == "rapid_table":
                _RAPID_TABLE_UNAVAILABLE = True
            raise

        config = RapidTableInput(model_type=ModelType.SLANETPLUS, use_ocr=True)
        _RAPID_TABLE_ENGINE = RapidTable(config)
    return _RAPID_TABLE_ENGINE


def _rect_from_cell(cell: Any, width: int, height: int, page: Any) -> list[float] | None:
    try:
        values = cell.tolist() if hasattr(cell, "tolist") else cell
        points = [point for point in values if isinstance(point, (list, tuple))]
        if len(points) >= 2 and all(len(point) >= 2 for point in points):
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
        elif isinstance(values, (list, tuple)) and len(values) == 4:
            xs = [float(values[0]), float(values[2])]
            ys = [float(values[1]), float(values[3])]
        else:
            return None
        x0, x1 = max(0.0, min(xs)), min(float(width), max(xs))
        y0, y1 = max(0.0, min(ys)), min(float(height), max(ys))
        if x1 <= x0 or y1 <= y0:
            return None
        sx = float(page.rect.width) / max(1, width)
        sy = float(page.rect.height) / max(1, height)
        return [
            round(float(page.rect.x0) + x0 * sx, 2),
            round(float(page.rect.y0) + y0 * sy, 2),
            round(float(page.rect.x0) + x1 * sx, 2),
            round(float(page.rect.y0) + y1 * sy, 2),
        ]
    except (TypeError, ValueError, IndexError):
        return None


def extract_rapid_table_fallback(page: Any) -> dict[str, Any] | None:
    """Use table structure only on OCR pages, reusing their OCR word boxes.

    Any dependency/model/runtime problem returns ``None`` so the established
    geometry path remains authoritative and the page result is unchanged.
    """
    if _RAPID_TABLE_UNAVAILABLE:
        return None
    entries = getattr(page, "_ocr_entries", None)
    if not entries:
        return None
    image_bytes = page.get_table_image_bytes()
    if not image_bytes:
        return None
    temp_path: Path | None = None
    try:
        import numpy as np

        boxes = np.asarray([entry["points"] for entry in entries], dtype="float32")
        texts = tuple(entry["text"] for entry in entries)
        scores = tuple(float(entry.get("confidence", 0.0)) for entry in entries)
        with tempfile.NamedTemporaryFile(prefix="docuflow_table_", suffix=".png", delete=False) as image_file:
            temp_path = Path(image_file.name)
            image_file.write(image_bytes)
        with _RAPID_TABLE_LOCK:
            result = _get_engine()(
                str(temp_path),
                ocr_results=[(boxes, texts, scores)],
                batch_size=1,
            )

        html_candidates = getattr(result, "pred_htmls", None)
        if html_candidates is None or len(html_candidates) == 0 or not isinstance(html_candidates[0], str):
            return None
        parser = _TableHTMLParser()
        parser.feed(html_candidates[0])
        if not parser.rows:
            return None

        first_row_is_header = bool(parser.rows[0]) and all(
            cell["kind"] == "th" for cell in parser.rows[0]
        )
        headers = [cell["text"] for cell in parser.rows[0]] if first_row_is_header else []
        body_rows = parser.rows[1:] if first_row_is_header else parser.rows
        column_count = max(
            len(headers), max((len(row) for row in body_rows), default=0)
        )
        if column_count < 2 or not body_rows:
            return None
        headers.extend([""] * (column_count - len(headers)))
        rows = [
            [row[index]["text"] if index < len(row) else "" for index in range(column_count)]
            for row in body_rows
        ]

        raw_cell_boxes = getattr(result, "cell_bboxes", None)
        flat_boxes = (
            list(raw_cell_boxes[0])
            if raw_cell_boxes is not None and len(raw_cell_boxes) > 0 else []
        )
        pixel_width = int(getattr(page, "_image_width", 0))
        pixel_height = int(getattr(page, "_image_height", 0))
        converted_boxes = [
            _rect_from_cell(box, pixel_width, pixel_height, page)
            for box in flat_boxes
        ]
        if not any(converted_boxes):
            return None

        table_cells = len(parser.rows[0]) if parser.rows else column_count
        header_boxes = converted_boxes[:table_cells] if first_row_is_header else []
        body_offset = table_cells if first_row_is_header else 0
        cell_bboxes: list[list[list[float] | None]] = []
        row_bboxes: list[list[float] | None] = []
        for row_index, row in enumerate(body_rows):
            cells = [
                converted_boxes[body_offset + sum(len(item) for item in body_rows[:row_index]) + column]
                if body_offset + sum(len(item) for item in body_rows[:row_index]) + column < len(converted_boxes)
                else None
                for column in range(column_count)
            ]
            cell_bboxes.append(cells)
            present = [box for box in cells if box]
            row_bboxes.append([
                min(box[0] for box in present), min(box[1] for box in present),
                max(box[2] for box in present), max(box[3] for box in present),
            ] if present else None)
        all_boxes = [box for box in converted_boxes if box]
        source_bbox = [
            min(box[0] for box in all_boxes), min(box[1] for box in all_boxes),
            max(box[2] for box in all_boxes), max(box[3] for box in all_boxes),
        ]
        header_cells = [
            {
                "row": 0, "column": index, "text": headers[index],
                "rowspan": 1, "colspan": 1,
                "source_bbox": header_boxes[index] if index < len(header_boxes) else None,
            }
            for index in range(column_count)
        ]
        return {
            "table_role": "line_items",
            "layout_type": "item_table",
            "headers": headers,
            "header_rows": [headers],
            "header_cells": header_cells,
            "header_detected": first_row_is_header,
            "header_source": "rapid_table" if first_row_is_header else "not_detected",
            "rows": rows,
            "row_count": len(rows),
            "column_count": column_count,
            "source_bbox": source_bbox,
            "row_source_bboxes": row_bboxes,
            "cell_source_bboxes": cell_bboxes,
            "rows_need_review": [True] * len(rows),
            "row_review_reasons": [["rapid_table_structure_fallback"] for _ in rows],
            "review_required": True,
            "detection_confidence": 0.55,
            "recovery_strategy": "rapid_table_structure_fallback",
        }
    except Exception as exc:  # noqa: BLE001 - optional fallback must not break OCR extraction.
        logger.debug(
            "RapidTable fallback unavailable; retaining geometry extraction (%s)",
            type(exc).__name__,
        )
        return None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
