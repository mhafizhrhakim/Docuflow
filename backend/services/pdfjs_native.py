"""Fast, source-native text and grid extraction via the project's PDF.js runtime."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_BRIDGE = Path(__file__).with_name("pdfjs_table_bridge.mjs")


def inspect_with_pdfjs(pdf_bytes: bytes) -> tuple[list[dict[str, Any]], float] | None:
    """Return quick page text/grid data; failures leave the legacy path intact."""
    node = shutil.which("node")
    if not node or not _BRIDGE.is_file():
        return None
    try:
        started = time.perf_counter()
        completed = subprocess.run(
            [node, str(_BRIDGE)], input=pdf_bytes, capture_output=True,
            check=True, timeout=30,
        )
        payload = json.loads(completed.stdout)
        pages = payload.get("pages")
        return (pages, time.perf_counter() - started) if isinstance(pages, list) else None
    except Exception as exc:
        logger.info("PDF.js native acceleration unavailable: %s", type(exc).__name__)
        return None


def page_native_text(page_data: dict[str, Any]) -> str:
    return "\n".join(
        str(item.get("text", "")).strip()
        for item in page_data.get("text_items", [])
        if str(item.get("text", "")).strip()
    )


def _column_edges(rectangles: list[list[float]]) -> list[float]:
    frequencies: Counter[float] = Counter()
    for rect in rectangles:
        if len(rect) == 4:
            frequencies[round(float(rect[0]), 1)] += 1
            frequencies[round(float(rect[2]), 1)] += 1
    if not frequencies:
        return []
    common = max(frequencies.values())
    candidates = sorted(
        x for x, count in frequencies.items()
        if count >= common * 0.55
    )
    if len(candidates) < 3:
        return []
    # Merge nearly coincident stroke/fill edges from the same source boundary.
    edges: list[float] = []
    for value in candidates:
        if not edges or value - edges[-1] > 1.5:
            edges.append(value)
        elif frequencies[value] > frequencies[round(edges[-1], 1)]:
            edges[-1] = value
    right = max(float(rect[2]) for rect in rectangles if len(rect) == 4)
    if right - edges[-1] > 1.5:
        edges.append(round(right, 1))
    return edges


def _header_bands(
    rectangles: list[list[float]], edges: list[float],
) -> list[tuple[float, float]]:
    bands: dict[tuple[float, float], set[int]] = defaultdict(set)
    for x0, y0, x1, y1 in rectangles:
        if not (edges[0] - 1 <= x0 and x1 <= edges[-1] + 1):
            continue
        column = min(
            range(len(edges) - 1),
            key=lambda index: abs(edges[index] - x0),
        )
        if abs(edges[column] - x0) <= 1.5 and abs(edges[column + 1] - x1) <= 1.5:
            bands[(round(y0, 1), round(y1, 1))].add(column)
    minimum = max(2, len(edges) - 2)
    return sorted(
        (y0, y1) for (y0, y1), columns in bands.items()
        if len(columns) >= minimum
    )


def _line_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: (float(value["y0"]), float(value["x0"]))):
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        y = float(item["y1"])
        if lines and abs(y - lines[-1]["y"]) <= 2.5:
            line = lines[-1]
            line["items"].append(item)
            line["y"] = sum(float(part["y1"]) for part in line["items"]) / len(line["items"])
        else:
            lines.append({"y": y, "items": [item]})
    return lines


def extract_grid_table(page_data: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild a lined native table from PDF.js text boxes and cell rectangles.

    This fast path is deliberately conservative: it requires repeated source
    cell geometry, a distributed header band, and repeated first-column row IDs.
    All other pages continue through the existing native/OCR extractor.
    """
    rectangles = page_data.get("rectangles") or []
    items = page_data.get("text_items") or []
    edges = _column_edges(rectangles)
    if len(edges) < 3 or len(items) < 8:
        return None
    bands = _header_bands(rectangles, edges)
    if not bands:
        return None
    column_count = len(edges) - 1
    lines = _line_groups(items)

    # The fast grid emits flat one-row headers. If a short source label visibly
    # spans more than one cell in a header band, defer to PyMuPDF's native table
    # path, which preserves the source rowspan/colspan hierarchy.
    for band_top, band_bottom in bands:
        for item in items:
            center_y = (float(item["y0"]) + float(item["y1"])) / 2
            text = str(item.get("text", "")).strip()
            if not (band_top - 2 <= center_y <= band_bottom + 2):
                continue
            if len(text) > 60 or not re.search(r"[A-Za-zÀ-ž]", text):
                continue
            left, right = float(item["x0"]), float(item["x1"])
            covered = [
                index for index in range(column_count)
                if left < edges[index + 1] - 1 and right > edges[index] + 1
            ]
            if len(covered) > 1:
                return None

    def column_for(x: float) -> int | None:
        if x < edges[0] - 2 or x > edges[-1] + 2:
            return None
        return min(column_count - 1, max(0, bisect_right(edges, x) - 1))

    item_id = re.compile(r"(?:\d{1,4}[.)]?|[A-Za-z][.)]?|(?=[A-Za-z0-9./_-]{2,25}$)(?=.*\d)[A-Za-z0-9][A-Za-z0-9./_-]*)")
    first_width = edges[1] - edges[0]
    row_starts: list[tuple[int, str]] = []
    for line_index, line in enumerate(lines):
        first_column_text = " ".join(
            str(item["text"]).strip() for item in line["items"]
            if float(item["x0"]) <= edges[0] + first_width + 2
            and column_for(float(item["x0"])) == 0
        ).strip()
        if item_id.fullmatch(first_column_text):
            row_starts.append((line_index, first_column_text))
    # Two identifiers can still be a document header or a short cover-page
    # fragment. Leave those pages to the richer native extractor, which can
    # use surrounding row/header context before deciding they are line items.
    if len(row_starts) < 3:
        return None

    # A page can contain multiple ruled bands (form checkboxes, metadata, and
    # the actual item table). Pick the text band that is both header-like and
    # geometrically close to repeated source row identifiers. The previous
    # first-match rule could anchor to an unrelated form band and still gather
    # rows from a later table on the page.
    header_options: list[tuple[float, tuple[float, float], list[str]]] = []
    numeric = re.compile(r"^[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)%?$")
    for band in bands:
        candidate = [""] * column_count
        for item in items:
            center_y = (float(item["y0"]) + float(item["y1"])) / 2
            if band[0] - 2 <= center_y <= band[1] + 2:
                column = column_for(float(item["x0"]))
                if column is not None:
                    candidate[column] = (candidate[column] + " " + str(item["text"])).strip()
        populated_values = [value.strip() for value in candidate if value.strip()]
        if len(populated_values) < max(2, int(column_count * 0.45)):
            continue
        following_rows = [
            (lines[index]["y"] - band[1], identifier)
            for index, identifier in row_starts
            if lines[index]["y"] > band[1]
            and item_id.fullmatch(identifier)
        ]
        if len(following_rows) < 2:
            continue
        first_row_gap = min(gap for gap, _identifier in following_rows)
        band_height = max(1.0, band[1] - band[0])
        if first_row_gap > max(18.0, band_height * 3.0):
            continue
        label_count = sum(bool(re.search(r"[A-Za-zÀ-ž]", value)) for value in populated_values)
        numeric_count = sum(bool(numeric.fullmatch(value.replace(" ", ""))) for value in populated_values)
        # Header text tends to contain labels; data rows tend to contain numeric
        # values. This source-derived score helps reject ruled body rows that
        # otherwise satisfy the same occupancy threshold.
        score = label_count * 2.0 - numeric_count * 5.0 - first_row_gap * 0.5
        header_options.append((score, band, candidate))
    if not header_options:
        return None
    # Prefer the earliest credible header band on the page. Repeated table
    # headers often have the same text, and selecting a later one discards the
    # preceding valid rows. Unrelated form bands were already filtered by the
    # item-row proximity check above.
    _score, header_band, headers = min(header_options, key=lambda option: option[1][0])
    visible_headers = [value.strip() for value in headers if value.strip()]
    if not visible_headers:
        return None
    # A compact page title plus invoice/page identifiers can sit immediately
    # above body rows and mimic a distributed header band. Reject that fast
    # path when most selected labels contain digits; the native geometry path
    # can then use the actual table's column evidence.
    non_numeric_header_share = sum(
        not re.search(r"\d", value) for value in visible_headers
    ) / len(visible_headers)
    if non_numeric_header_share < 0.6:
        return None

    # Preserve stacked headers when their physical bands touch. For example,
    # "Item" above "No." belongs to the same observed source column.
    header_top = header_band[0]
    header_bottom = header_band[1]
    header_bands_used = [header_band]
    header_height = max(1.0, header_band[1] - header_band[0])
    for previous_band in reversed([band for band in bands if band[1] <= header_band[0]]):
        gap = header_top - previous_band[1]
        if gap > max(2.0, header_height * 0.35):
            break
        previous_values = [""] * column_count
        for item in items:
            center_y = (float(item["y0"]) + float(item["y1"])) / 2
            if previous_band[0] - 2 <= center_y <= previous_band[1] + 2:
                column = column_for(float(item["x0"]))
                if column is not None:
                    previous_values[column] = (previous_values[column] + " " + str(item["text"])).strip()
        previous_nonempty = [value for value in previous_values if value]
        if (
            len(previous_nonempty) < 2
            or any(len(value) > 60 for value in previous_nonempty)
            or sum(bool(re.search(r"[A-Za-zÀ-ž]", value)) for value in previous_nonempty) < 2
        ):
            break
        for column, value in enumerate(previous_values):
            if value and value.casefold() not in headers[column].casefold():
                headers[column] = f"{value} {headers[column]}".strip()
        header_top = previous_band[0]
        header_bands_used.append(previous_band)
        break

    # Attach the next touching physical band when it supplies another level of
    # short column labels (for example, Item / No. or Balance / New / Used).
    header_height = max(1.0, header_bottom - header_top)
    for following_band in [band for band in bands if band[0] >= header_bottom]:
        gap = following_band[0] - header_bottom
        if gap > max(2.0, header_height * 0.35):
            break
        following_values = [""] * column_count
        for item in items:
            center_y = (float(item["y0"]) + float(item["y1"])) / 2
            if following_band[0] - 2 <= center_y <= following_band[1] + 2:
                column = column_for(float(item["x0"]))
                if column is not None:
                    following_values[column] = (following_values[column] + " " + str(item["text"])).strip()
        nonempty = [value for value in following_values if value]
        if (
            len(nonempty) < 2
            or any(len(value) > 60 for value in nonempty)
            or sum(bool(re.search(r"[A-Za-zÀ-ž]", value)) for value in nonempty) < 2
        ):
            break
        for column, value in enumerate(following_values):
            if value and value.casefold() not in headers[column].casefold():
                headers[column] = f"{headers[column]} {value}".strip()
        header_bottom = following_band[1]
        header_bands_used.append(following_band)
        break

    # Repeated source headers may occur between body rows. Exclude only bands
    # whose positioned labels actually match this table's selected headers;
    # treating every grid band as a header hides legitimate first data rows.
    header_tokens = [re.sub(r"[^a-z0-9]", "", value.casefold()) for value in headers]
    for band in bands:
        if band in header_bands_used:
            continue
        band_values = [""] * column_count
        for item in items:
            center_y = (float(item["y0"]) + float(item["y1"])) / 2
            if band[0] - 2 <= center_y <= band[1] + 2:
                column = column_for(float(item["x0"]))
                if column is not None:
                    band_values[column] = (band_values[column] + " " + str(item["text"])).strip()
        nonempty = [(index, value.strip()) for index, value in enumerate(band_values) if value.strip()]
        numeric_count = sum(bool(numeric.fullmatch(value.replace(" ", ""))) for _index, value in nonempty)
        matches = sum(
            bool(header_tokens[index])
            and (
                re.sub(r"[^a-z0-9]", "", value.casefold()) in header_tokens[index]
                or header_tokens[index] in re.sub(r"[^a-z0-9]", "", value.casefold())
            )
            for index, value in nonempty
        )
        if numeric_count == 0 and matches >= max(2, int(len(nonempty) * 0.55)):
            header_bands_used.append(band)

    rows: list[list[str]] = []
    row_boxes: list[list[float]] = []
    row_review: list[bool] = []
    row_reasons: list[list[str]] = []
    start_lines = [lines[index] for index, _identifier in row_starts]
    assigned_lines: list[list[dict[str, Any]]] = [[] for _ in row_starts]
    row_gaps = [
        start_lines[index + 1]["y"] - start_lines[index]["y"]
        for index in range(len(start_lines) - 1)
        if start_lines[index + 1]["y"] > start_lines[index]["y"]
    ]
    typical_row_gap = sorted(row_gaps)[len(row_gaps) // 2] if row_gaps else 20.0
    continuation_distance = max(12.0, typical_row_gap * 0.75)
    for line in lines:
        if line["y"] <= header_band[1] or any(
            band[0] - 1 <= line["y"] <= band[1] + 1 for band in header_bands_used
        ):
            continue
        nearest = min(
            range(len(start_lines)),
            key=lambda index: abs(line["y"] - start_lines[index]["y"]),
        )
        if abs(line["y"] - start_lines[nearest]["y"]) > continuation_distance:
            continue
        assigned_lines[nearest].append(line)

    for position, (_start_index, identifier) in enumerate(row_starts):
        values = [""] * column_count
        included: list[dict[str, Any]] = []
        for line in sorted(assigned_lines[position], key=lambda item: item["y"]):
            for item in line["items"]:
                x = float(item["x0"])
                column = column_for(x)
                if column is None:
                    continue
                text = str(item["text"]).strip()
                if text:
                    values[column] = (values[column] + " " + text).strip()
                    included.append(item)
        values[0] = identifier
        populated = sum(bool(value.strip()) for value in values)
        if populated < 2:
            continue
        rows.append(values)
        if included:
            row_boxes.append([
                min(float(item["x0"]) for item in included),
                min(float(item["y0"]) for item in included),
                max(float(item["x1"]) for item in included),
                max(float(item["y1"]) for item in included),
            ])
        else:
            row_boxes.append([edges[0], lines[start_index]["y"], edges[-1], lines[start_index]["y"] + 10])
        row_review.append(False)
        row_reasons.append([])

    if len(rows) < 2 or len(headers) != column_count:
        return None
    populated_rows = sum(sum(bool(cell) for cell in row) >= 2 for row in rows)
    if populated_rows / len(rows) < 0.9:
        return None
    numeric_rows = 0
    numeric_pattern = re.compile(r"^[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)%?$")
    for row in rows:
        numeric_cells = sum(
            bool(numeric_pattern.fullmatch(cell.strip().replace(" ", "")))
            for cell in row[max(1, column_count // 2):]
        )
        numeric_rows += numeric_cells >= 2
    if numeric_rows / len(rows) < 0.60:
        return None

    nonempty_headers = sum(bool(label.strip()) for label in headers)
    header_detected = nonempty_headers >= max(2, int(column_count * 0.45))
    header_cells = [
        {"row": 0, "column": index, "text": label.strip(), "rowspan": 1,
         "colspan": 1, "source_bbox": f"{edges[index]} {header_top} {edges[index + 1]} {header_bottom}"}
        for index, label in enumerate(headers) if label.strip()
    ]
    return {
        "table_index": 1,
        "source_page": int(page_data.get("page_number") or 0),
        "table_role": "line_items",
        "layout_type": "item_table",
        "is_source_table": True,
        "headers": headers,
        "header_rows": [headers],
        "header_cells": header_cells,
        "header_detected": header_detected,
        "header_source": "pdfjs_cell_geometry" if header_detected else "unavailable",
        "column_count": column_count,
        "row_count": len(rows),
        "rows": rows,
        "source_bbox": [edges[0], header_top, edges[-1], max(box[3] for box in row_boxes)],
        "header_source_bbox": [edges[0], header_top, edges[-1], header_bottom],
        "header_source_bboxes": [[edges[0], header_top, edges[-1], header_bottom]],
        "row_source_bboxes": row_boxes,
        "cell_review_flags": [[False] * column_count for _ in rows],
        "rows_need_review": row_review,
        "row_review_reasons": row_reasons,
        "review_required": False,
        "review_issues": [],
        "detection_strategy": "pdfjs_source_grid",
        "detection_confidence": 0.85 if header_detected else 0.65,
    }
