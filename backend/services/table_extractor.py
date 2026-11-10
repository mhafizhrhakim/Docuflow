"""Layout-aware native PDF table and summary extraction.

The extractor prefers PyMuPDF's line-based table structure when credible. For
PDFs without table rules, it falls back to span geometry: repeated item-row
baselines establish the data rows, and the closest preceding distributed text
band supplies document-derived column labels. Summary label/value pairs are
recovered separately based on their visual alignment below the item region;
no vendor, filename, fixed header schema, or total-label whitelist is required.

For scanned pages, the same geometric routines can consume OCR word boxes.
A repeated-row fallback handles scans where OCR fragments the column header.
Ambiguous layouts should be reviewed rather than treated as authoritative.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from decimal import Decimal, InvalidOperation
import logging
import math
import re
import time
from statistics import median
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# Numeric tokens used for geometric classification, not to infer column names.
_NUMERIC_TOKEN_RE = re.compile(r"^[()\-−+]?\d[\d,]*(?:\.\d+)?[)]?$")
# A summary value is deliberately stricter than a generic number so dates, item
# numbers, bank account numbers, and quantities are not mistaken for amounts.
_MONEY_TOKEN_RE = re.compile(
    # Accept common formatted money values with or without a decimal component.
    # Grouping separators or a decimal fraction help distinguish amounts from
    # dates, item IDs, and unformatted account numbers.
    r"(?<![A-Za-z0-9/])(?:[$€£¥₹]\s*)?\(?-?(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,4})?|\d+\.\d{2,4})\)?(?![A-Za-z0-9/])"
)


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _clean_rows(raw_rows: Iterable[Iterable[Any]]) -> list[list[str]]:
    return [[_clean_cell(cell) for cell in row] for row in raw_rows]


def _split_repeated_quantity_measure(table: dict[str, Any]) -> None:
    """Separate a repeatedly combined numeric quantity and unit from one cell.

    This is enabled only when the source has a quantity header, no independent
    unit header, and multiple rows confirm the same number-plus-token shape.
    The unit token is learned from the source values; no unit vocabulary is
    maintained here. Rows without a recoverable unit remain visible and are
    marked for review.
    """
    headers = [str(value or "").strip() for value in table.get("headers", [])]
    normalized = [re.sub(r"[^a-z]", "", value.casefold()) for value in headers]
    quantity_indexes = [index for index, label in enumerate(normalized)
                        if label in {"qty", "quantity", "quantities"}]
    unit_indexes = [index for index, label in enumerate(normalized)
                    if label in {"unit", "uom", "unitofmeasure", "measure"}]
    header_rows = table.get("header_rows") or [headers]
    if len(quantity_indexes) != 1 or unit_indexes or len(header_rows) != 1:
        return

    quantity_index = quantity_indexes[0]
    rows = table.get("rows") or []
    measure_pattern = re.compile(
        r"^([+-]?\d[\d,.]*)(?:\s+)([A-Za-zÀ-ž][A-Za-zÀ-ž0-9/.-]*)$"
    )
    parsed = [
        measure_pattern.fullmatch(str(row[quantity_index] or "").strip())
        if quantity_index < len(row) else None
        for row in rows
    ]
    nonempty = sum(bool(quantity_index < len(row) and str(row[quantity_index] or "").strip())
                   for row in rows)
    confirmed = sum(match is not None for match in parsed)
    if confirmed < 2 or confirmed < max(2, (nonempty + 1) // 2):
        return

    insert_at = quantity_index + 1
    table["headers"].insert(insert_at, "Unit")
    table["header_rows"] = [[*header_rows[0][:insert_at], "Unit", *header_rows[0][insert_at:]]]
    for row_index, row in enumerate(rows):
        while len(row) < len(headers):
            row.append("")
        match = parsed[row_index]
        unit_value = match.group(2) if match else ""
        if match:
            row[quantity_index] = match.group(1)
        row.insert(insert_at, unit_value)

    for cell in table.get("header_cells", []):
        if int(cell.get("column", -1)) >= insert_at:
            cell["column"] = int(cell["column"]) + 1
    table.setdefault("header_cells", []).append({
        "row": 0, "column": insert_at, "text": "Unit", "rowspan": 1,
        "colspan": 1, "source_bbox": None,
    })
    table["header_cells"].sort(key=lambda cell: (int(cell.get("row", 0)), int(cell.get("column", 0))))
    table["column_count"] = len(table["headers"])
    table["row_count"] = len(rows)
    if any(match is None for match in parsed):
        review = table.setdefault("rows_need_review", [False] * len(rows))
        reasons = table.setdefault("row_review_reasons", [[] for _ in rows])
        for index, match in enumerate(parsed):
            if match is None:
                review[index] = True
                reasons[index] = [*reasons[index], "quantity_unit_not_separated"]


def _split_merged_identifier_description_table(table: dict[str, Any]) -> bool:
    """Split a source-derived identifier+description column when OCR merged it.

    This runs only when the extracted source header itself explicitly contains
    an identifier label and an item/description label in one cell, while a
    separate quantity label occupies the final column. It does not assign a
    schema to arbitrary two-column tables.
    """
    headers = [_clean_cell(item) for item in table.get("headers", [])]
    if len(headers) != 2:
        return False

    merged_header = headers[0]
    quantity_header = headers[1]
    identifier_header_match = re.match(
        r"^(?P<identifier>.*?\b(?:no\.?|number|#))\s+"
        r"(?P<item>(?:item|description|particulars|details|product|goods)\b.*)$",
        merged_header,
        flags=re.IGNORECASE,
    )
    if not identifier_header_match:
        return False
    if not re.search(r"\b(?:qty|quantity)\b", quantity_header, flags=re.IGNORECASE):
        return False

    raw_rows = table.get("rows", [])
    if not isinstance(raw_rows, list) or len(raw_rows) < 2:
        return False

    parsed_rows: list[list[str]] = []
    split_count = 0
    for row in raw_rows:
        cells = [_clean_cell(value) for value in row] if isinstance(row, (list, tuple)) else []
        first = cells[0] if cells else ""
        rest = cells[1:] if len(cells) > 1 else [""]
        match = re.match(r"^\s*(\d{1,4})(?:[.)])?\s+(.+?)\s*$", first)
        if match:
            parsed_rows.append([match.group(1), match.group(2).strip(), *rest])
            split_count += 1
        else:
            # Keep the row without fabricating an identifier. Place its source
            # text in the description lane and leave the identifier empty.
            parsed_rows.append(["", first, *rest])

    nonempty_count = sum(
        1 for row in raw_rows
        if isinstance(row, (list, tuple)) and any(_clean_cell(value) for value in row)
    )
    if nonempty_count == 0 or split_count / nonempty_count < 0.70:
        return False

    identifier_header = identifier_header_match.group("identifier").strip()
    item_header = identifier_header_match.group("item").strip()
    new_headers = [identifier_header, item_header, quantity_header]

    table["headers"] = new_headers
    table["header_rows"] = [new_headers]
    table["header_cells"] = [
        {"row": 0, "column": index, "text": label, "rowspan": 1, "colspan": 1, "source_bbox": None}
        for index, label in enumerate(new_headers)
    ]
    table["column_count"] = 3
    table["rows"] = parsed_rows
    table["row_count"] = len(parsed_rows)
    table["header_detected"] = True
    table["header_source"] = "split_from_source_header"
    table["row_normalization"] = "split_identifier_from_merged_description"
    return True


def _is_probable_column_header_line(line: dict[str, Any]) -> bool:
    """Reject page masthead/contact lines from OCR-derived table headers."""
    runs = line.get("runs", [])
    texts = [_clean_cell(run.get("text", "")) for run in runs]
    texts = [value for value in texts if value]
    if len(texts) < 2:
        return False

    combined = " ".join(texts)
    lowered = combined.casefold()
    # These markers identify contact or address bands, not column labels.
    if any(marker in lowered for marker in ("@", "email", "e-mail", "phone", "telephone", "fax", "http://", "https://")):
        return False
    if re.search(r"\b(?:ph|tel)\s*[:.]", lowered):
        return False
    if len(combined) > 90 or len(combined.split()) > 12:
        return False

    short_label_count = sum(
        1 for value in texts
        if len(value) <= 28 and re.search(r"[A-Za-zÀ-ž]", value)
    )
    return short_label_count >= 2


def _is_numeric_token(value: str) -> bool:
    return bool(_NUMERIC_TOKEN_RE.fullmatch(_clean_cell(value)))


_OCR_NUMERIC_EDGE_QUOTES = "'`‘’\"“”"
_OCR_NUMERIC_CONFUSABLES = str.maketrans({"S": "5", "s": "5", "O": "0", "o": "0"})


def _ocr_numeric_recovery_candidate(value: Any) -> str | None:
    """Return an unambiguous numeric reading of common OCR character noise.

    This is deliberately limited to numeric-shaped cells: edge quote marks are
    ignored and S/O are mapped only when the resulting token is a valid number.
    Callers still need independent numeric-column evidence before applying it.
    """
    text = _clean_cell(value)
    normalized = text.strip(_OCR_NUMERIC_EDGE_QUOTES).translate(_OCR_NUMERIC_CONFUSABLES)
    if normalized == text or not re.search(r"\d", text):
        return None
    if not re.fullmatch(r"[0-9.,()+\-−]+", normalized) or not _is_numeric_token(normalized):
        return None
    return normalized


def _is_numeric_geometry_token(value: Any) -> bool:
    """Recognize OCR numeric tokens with harmless quote marks at their edges."""
    return _is_numeric_token(value) or _ocr_numeric_recovery_candidate(value) is not None


def _bbox_overlap_ratio(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Return overlap area divided by the smaller bbox area."""
    if len(left) != 4 or len(right) != 4:
        return 0.0
    ix0, iy0 = max(left[0], right[0]), max(left[1], right[1])
    ix1, iy1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    smaller_area = min(left_area, right_area)
    return intersection / smaller_area if smaller_area else 0.0


def _find_header_index(rows: list[list[str]]) -> int | None:
    """Find a compact, label-dense row without relying on fixed header names."""
    best_index: int | None = None
    best_score = 0.0
    for index, row in enumerate(rows[:10]):
        populated = [cell.strip() for cell in row if cell and cell.strip()]
        if len(populated) < 3:
            continue
        short_labels = sum(1 for cell in populated if len(cell) <= 20)
        numeric_cells = sum(1 for cell in populated if _is_numeric_token(cell))
        long_sentences = sum(1 for cell in populated if len(cell) > 45)
        # Header rows are normally spread across multiple columns, mostly short,
        # non-numeric labels. This is a shape test, not a list of known headers.
        if short_labels < 3 or numeric_cells > 0 or long_sentences > 0:
            continue
        score = short_labels + min(len(populated), 10) * 0.25 - index * 0.15
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _word_runs_on_visual_baselines(page: Any) -> list[dict[str, Any]]:
    """Group words into visual baselines, then split each baseline by large gaps."""
    try:
        raw_words = page.get_text("words", sort=True)
    except Exception:
        logger.exception("Unable to read word positions from PDF page")
        return []

    words = [
        {
            "x0": float(word[0]), "y0": float(word[1]),
            "x1": float(word[2]), "y1": float(word[3]),
            "text": str(word[4]).strip(),
        }
        for word in raw_words if len(word) >= 5 and str(word[4]).strip()
    ]
    words.sort(key=lambda item: ((item["y0"] + item["y1"]) / 2, item["x0"]))

    baselines: list[dict[str, Any]] = []
    baseline_tolerance = 4.0 if getattr(page, "ocr_word_count", 0) else 2.5
    for word in words:
        center_y = (word["y0"] + word["y1"]) / 2
        if baselines and abs(center_y - baselines[-1]["y"]) <= baseline_tolerance:
            baseline = baselines[-1]
            baseline["words"].append(word)
            centers = [(item["y0"] + item["y1"]) / 2 for item in baseline["words"]]
            baseline["y"] = float(median(centers))
        else:
            baselines.append({"y": center_y, "words": [word]})

    # On scanned pages, OCR often places a left-side item number on a nearby
    # baseline from the same physical row (the description and numeric cells
    # can be 6–9 pt higher because of skew or cell vertical alignment). Keep
    # that identifier with the row whose text and right-side values share its
    # horizontal span. Do not widen the global baseline tolerance: that would
    # merge genuine wrapped description lines on dense tables.
    if getattr(page, "ocr_word_count", 0) and len(baselines) > 1:
        page_width = float(page.rect.width)
        for baseline_index, baseline in enumerate(baselines):
            identifiers = [
                word for word in baseline["words"]
                if float(word["x0"]) <= page_width * 0.16
                and _is_item_identifier(str(word["text"]))
            ]
            if not identifiers:
                continue
            neighboring = []
            for target_index in (baseline_index - 1, baseline_index + 1):
                if not 0 <= target_index < len(baselines):
                    continue
                target = baselines[target_index]
                vertical_gap = abs(float(target["y"]) - float(baseline["y"]))
                target_words = target["words"]
                right_words = [word for word in target_words if float(word["x0"]) > page_width * 0.28]
                right_numeric_words = [word for word in right_words if _is_numeric_token(str(word["text"]))]
                x_span = (
                    max((float(word["x1"]) for word in target_words), default=0.0)
                    - min((float(word["x0"]) for word in target_words), default=0.0)
                )
                if (
                    4.5 <= vertical_gap <= 10.0
                    and len(right_words) >= 2
                    and len(right_numeric_words) >= 2
                    and x_span >= page_width * 0.48
                ):
                    neighboring.append((vertical_gap, target_index))
            if neighboring:
                _gap, target_index = min(neighboring)
                target = baselines[target_index]
                for word in identifiers:
                    baseline["words"].remove(word)
                    target["words"].append(word)
                if not baseline["words"]:
                    baseline["remove"] = True
                centers = [
                    (word["y0"] + word["y1"]) / 2 for word in target["words"]
                ]
                target["y"] = float(median(centers))
        baselines = [baseline for baseline in baselines if not baseline.get("remove")]

    output: list[dict[str, Any]] = []
    for baseline in baselines:
        ordered = sorted(baseline["words"], key=lambda item: item["x0"])
        heights = [max(1.0, item["y1"] - item["y0"]) for item in ordered]
        gap_limit = max(5.5, float(median(heights)) * 0.9)
        runs: list[list[dict[str, Any]]] = []
        for word in ordered:
            if not runs or word["x0"] - runs[-1][-1]["x1"] > gap_limit:
                runs.append([word])
            else:
                runs[-1].append(word)
        run_payloads = []

        def make_run_payload(run_words: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "text": " ".join(word["text"] for word in run_words).strip(),
                "x0": min(word["x0"] for word in run_words),
                "y0": min(word["y0"] for word in run_words),
                "x1": max(word["x1"] for word in run_words),
                "y1": max(word["y1"] for word in run_words),
                "words": run_words,
            }

        for run in runs:
            # OCR can join a compact row identifier and its description when
            # the gap is slightly smaller than the detected word spacing. Keep
            # those as separate geometric runs so the identifier can anchor a
            # row and the description can occupy its own observed lane.
            if (
                getattr(page, "ocr_word_count", 0)
                and len(run) >= 2
                and float(run[0]["x0"]) <= float(page.rect.width) * 0.42
                and _is_item_identifier(run[0]["text"])
                and any(re.search(r"[A-Za-zÀ-ž]", word["text"]) for word in run[1:])
            ):
                run_payloads.append(make_run_payload(run[:1]))
                run_payloads.append(make_run_payload(run[1:]))
            else:
                run_payloads.append(make_run_payload(run))
        output.append({
            "y": baseline["y"],
            "x0": min(item["x0"] for item in ordered),
            "x1": max(item["x1"] for item in ordered),
            "words": ordered,
            "runs": run_payloads,
        })
    return output


def _without_browser_print_footer_lines(
    lines: list[dict[str, Any]], page_height: float,
) -> list[dict[str, Any]]:
    """Remove a browser-generated about:blank footer from OCR table geometry."""
    retained: list[dict[str, Any]] = []
    for line in lines:
        if float(line.get("y", 0.0)) < page_height * 0.82:
            retained.append(line)
            continue
        footer_tokens = {
            re.sub(r"[^a-z]", "", _clean_cell(run.get("text", "")).casefold())
            for run in line.get("runs", [])
        }
        if "aboutblank" in footer_tokens:
            continue
        retained.append(line)
    return retained


def _is_currency_marker(value: str) -> bool:
    """Recognize standalone currency markers without listing invoice labels."""
    cleaned = _clean_cell(value).strip(" _-:")
    return bool(
        re.fullmatch(r"\(?[A-Z]{3}\)?", cleaned)
        or re.fullmatch(r"[$€£¥₹]", cleaned)
    )


def _find_summary_candidates(
    page: Any,
    visual_lines: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Find positioned label/value pairs using numeric formatting and geometry.

    No summary labels are whitelisted. Formatted amounts are paired with the
    closest text run on their left. A standalone currency marker between the
    label and amount is folded back into the label rather than mistaken for it.
    """
    lines = visual_lines if visual_lines is not None else _word_runs_on_visual_baselines(page)
    summaries: list[dict[str, Any]] = []
    for line in lines:
        runs = line["runs"]
        for index, run in enumerate(runs):
            matches = list(_MONEY_TOKEN_RE.finditer(run["text"]))
            if not matches or index == 0:
                continue
            value_match = matches[-1]
            value = value_match.group(0).strip()
            label_run = runs[index - 1]
            label = _clean_cell(label_run["text"]).strip(" _-:")
            label_start_x = float(label_run["x0"])
            # Some layouts put a currency code/symbol in its own visual cell:
            # e.g. TOTAL AMOUNT | (YEN) | 2,040,000. Preserve the label text.
            if _is_currency_marker(label) and index >= 2:
                previous = _clean_cell(runs[index - 2]["text"]).strip(" _-:")
                if re.search(r"[A-Za-zÀ-ž]", previous):
                    label = f"{previous} ({label.strip('()')})".strip()
                    label_start_x = float(runs[index - 2]["x0"])
            if not label or not re.search(r"[A-Za-zÀ-ž]", label):
                continue

            # Avoid treating multiple amounts on one baseline as one summary:
            # only pair a label with a value when no later formatted amount is
            # present. This is useful for totals aligned at the far right.
            trailing = runs[index + 1:]
            if any(_MONEY_TOKEN_RE.search(item["text"]) for item in trailing):
                continue
            gap = float(run["x0"]) - float(label_run["x1"])
            median_height = median([max(1.0, w["y1"] - w["y0"]) for w in line["words"]])
            # A generous geometric limit supports distant right-aligned totals,
            # while still requiring a plausible label/value relationship.
            if gap < -2 or gap > max(250.0, median_height * 18.0):
                continue
            bbox = [
                round(min(label_start_x, float(run["x0"])), 2),
                round(min(float(label_run["y0"]), float(run["y0"])), 2),
                round(max(float(label_run["x1"]), float(run["x1"])), 2),
                round(max(float(label_run["y1"]), float(run["y1"])), 2),
            ]
            summaries.append({
                "label": label.rstrip(" :\t–—-"),
                "value": value,
                "source_page": int(getattr(page, "number", 0)) + 1,
                "source_bbox": bbox,
                "_y": float(line["y"]),
            })
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for item in sorted(summaries, key=lambda current: (current["_y"], current["source_bbox"][0])):
        key = (item["label"].casefold(), item["value"], round(item["_y"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _native_table_candidates(
    page: Any,
    methods_attempted: list[str],
    methods_failed: list[str],
    *,
    strategies: tuple[str, ...] = ("lines", "text"),
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_bboxes: list[tuple[float, ...]] = []
    page_area = max(1.0, float(page.rect.width) * float(page.rect.height))

    for strategy in strategies:
        methods_attempted.append(strategy)
        try:
            finder = page.find_tables(strategy=strategy)
            for found in (getattr(finder, "tables", []) or []):
                bbox = tuple(float(value) for value in (getattr(found, "bbox", ()) or ()))
                if len(bbox) == 4:
                    area_ratio = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1]) / page_area
                    vertical_ratio = max(0.0, bbox[3] - bbox[1]) / max(1.0, float(page.rect.height))
                else:
                    area_ratio, vertical_ratio = 1.0, 1.0
                # Text strategy frequently treats the entire page as a table on
                # borderless documents. Reject such page-sized candidates and
                # let the geometric fallback build a narrower item table.
                if strategy == "text" and (area_ratio >= 0.76 or vertical_ratio >= 0.82):
                    continue
                if any(
                    _bbox_overlap_ratio(bbox, previous) >= 0.86
                    or (len(bbox) == 4 and len(previous) == 4 and sum(abs(a-b) for a,b in zip(bbox, previous)) <= 6.0)
                    for previous in seen_bboxes
                ):
                    continue
                try:
                    raw_rows = found.extract()
                except Exception:
                    logger.exception("Failed extracting a detected PDF table (%s)", strategy)
                    continue
                rows = _clean_rows(raw_rows or [])
                if not rows:
                    continue
                row_bboxes: list[tuple[float, ...] | None] = []
                cell_bboxes: list[list[tuple[float, ...] | None]] = []
                for row_obj in (getattr(found, "rows", []) or []):
                    row_bbox = getattr(row_obj, "bbox", None)
                    row_bboxes.append(tuple(float(value) for value in row_bbox) if row_bbox and len(row_bbox) == 4 else None)
                    row_cells: list[tuple[float, ...] | None] = []
                    for cell in (getattr(row_obj, "cells", []) or []):
                        row_cells.append(tuple(float(value) for value in cell) if cell and len(cell) == 4 else None)
                    cell_bboxes.append(row_cells)
                # PyMuPDF should return one row bbox and one cell-geometry row
                # per extracted row, but keep this robust to version differences.
                while len(row_bboxes) < len(rows):
                    row_bboxes.append(None)
                while len(cell_bboxes) < len(rows):
                    cell_bboxes.append([])
                seen_bboxes.append(bbox)
                candidates.append({
                    "bbox": bbox, "rows": rows, "row_bboxes": row_bboxes[:len(rows)],
                    "cell_bboxes": cell_bboxes[:len(rows)],
                    "strategy": strategy,
                    "column_count": int(getattr(found, "col_count", 0) or 0),
                })
        except Exception as exc:
            methods_failed.append(f"{strategy}: {type(exc).__name__}: {exc}")
            logger.debug("Native table detection strategy %s failed", strategy, exc_info=True)
    return candidates


def _horizontal_rule_spans(page: Any) -> list[tuple[float, float, float]]:
    """Return merged horizontal vector rules as ``(y, x0, x1)`` spans."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    segments: list[tuple[float, float, float]] = []
    for drawing in drawings:
        for item in drawing.get("items", []):
            if not item or item[0] != "l" or len(item) < 3:
                continue
            start, end = item[1], item[2]
            if abs(float(start.y) - float(end.y)) > 0.8:
                continue
            x0, x1 = sorted((float(start.x), float(end.x)))
            if x1 - x0 >= max(24.0, float(page.rect.width) * 0.04):
                segments.append(((float(start.y) + float(end.y)) / 2.0, x0, x1))

    by_y: list[list[tuple[float, float, float]]] = []
    for segment in sorted(segments, key=lambda value: (value[0], value[1])):
        if not by_y or abs(segment[0] - by_y[-1][0][0]) > 1.2:
            by_y.append([segment])
        else:
            by_y[-1].append(segment)

    spans: list[tuple[float, float, float]] = []
    for group in by_y:
        y = float(median([segment[0] for segment in group]))
        intervals: list[list[float]] = []
        for _line_y, x0, x1 in sorted(group, key=lambda value: value[1]):
            if not intervals or x0 > intervals[-1][1] + 2.5:
                intervals.append([x0, x1])
            else:
                intervals[-1][1] = max(intervals[-1][1], x1)
        spans.extend((y, interval[0], interval[1]) for interval in intervals)
    return spans


def _extract_ruled_header_only_table(
    page: Any,
    visual_lines: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Keep a source table whose distributed header is ruled but has no rows.

    This fallback is intentionally limited to native text pages and headers
    bracketed by matching horizontal rules. It records only visible headings;
    it never invents empty item records or a document-specific schema.
    """
    if getattr(page, "ocr_word_count", 0):
        return None

    page_width = float(page.rect.width)
    rules = _horizontal_rule_spans(page)
    if len(rules) < 2:
        return None

    candidates = _candidate_header_records(visual_lines, page_width)
    bracketed: list[tuple[float, dict[str, Any], tuple[float, float, float], tuple[float, float, float]]] = []
    for candidate in candidates:
        runs = candidate["runs"]
        if len(runs) < 3 or not _is_probable_column_header_line({"runs": runs}):
            continue
        if float(candidate.get("coverage") or 0.0) < 0.35:
            continue
        header_top = min(float(run["y0"]) for run in runs)
        header_bottom = max(float(run["y1"]) for run in runs)
        header_x0 = min(float(run["x0"]) for run in runs)
        header_x1 = max(float(run["x1"]) for run in runs)

        top_rules = [
            rule for rule in rules
            if 0.0 <= header_top - rule[0] <= 24.0
            and rule[1] <= header_x0 + 4.0
            and rule[2] >= header_x1 - 4.0
        ]
        bottom_rules = [
            rule for rule in rules
            if -3.0 <= rule[0] - header_bottom <= 20.0
            and rule[1] <= header_x0 + 4.0
            and rule[2] >= header_x1 - 4.0
        ]
        for top_rule in top_rules:
            for bottom_rule in bottom_rules:
                band_height = bottom_rule[0] - top_rule[0]
                if not 8.0 <= band_height <= 48.0:
                    continue
                overlap = max(0.0, min(top_rule[2], bottom_rule[2]) - max(top_rule[1], bottom_rule[1]))
                shortest = max(1.0, min(top_rule[2] - top_rule[1], bottom_rule[2] - bottom_rule[1]))
                if overlap / shortest < 0.92:
                    continue
                score = (
                    float(candidate["coverage"]) * 10.0
                    + len(runs)
                    - abs(header_top - top_rule[0]) * 0.1
                    - abs(bottom_rule[0] - header_bottom) * 0.1
                )
                bracketed.append((score, candidate, top_rule, bottom_rule))

    if not bracketed:
        return None

    _score, candidate, top_rule, bottom_rule = max(bracketed, key=lambda item: item[0])
    headers = [_normalize_header_label(run["text"]) for run in candidate["runs"]]
    if len(headers) < 3 or sum(bool(value) for value in headers) < 3:
        return None

    header_cells = [
        {
            "row": 0,
            "column": index,
            "text": label,
            "rowspan": 1,
            "colspan": 1,
            "source_bbox": [
                round(float(run["x0"]), 2), round(float(run["y0"]), 2),
                round(float(run["x1"]), 2), round(float(run["y1"]), 2),
            ],
        }
        for index, (label, run) in enumerate(zip(headers, candidate["runs"]))
        if label
    ]
    header_bbox = [
        round(min(float(run["x0"]) for run in candidate["runs"]), 2),
        round(min(float(run["y0"]) for run in candidate["runs"]), 2),
        round(max(float(run["x1"]) for run in candidate["runs"]), 2),
        round(max(float(run["y1"]) for run in candidate["runs"]), 2),
    ]
    return {
        "table_index": 1,
        "source_page": int(getattr(page, "number", 0)) + 1,
        "table_role": "line_items",
        "layout_type": "item_table",
        "is_source_table": True,
        "headers": headers,
        "header_rows": [headers],
        "header_cells": header_cells,
        "header_detected": True,
        "header_source": "native_ruled_header_only",
        "column_count": len(headers),
        "row_count": 0,
        "rows": [],
        "source_bbox": [
            round(min(top_rule[1], bottom_rule[1]), 2),
            round(top_rule[0], 2),
            round(max(top_rule[2], bottom_rule[2]), 2),
            round(bottom_rule[0], 2),
        ],
        "header_source_bbox": header_bbox,
        "header_source_bboxes": [header_bbox],
        "row_source_bboxes": [],
        "context_rows": [],
        "summary_items": [],
        "metadata_items": [],
        "detection_strategy": "native_ruled_header_only",
        "detection_confidence": 0.78,
    }


def _row_population_threshold(column_count: int) -> int:
    # A candidate item row should occupy multiple columns. This eliminates
    # merged footer/legal/bank blocks without matching their text labels.
    return max(2, min(column_count, int((column_count * 0.45) + 0.999)))


def _normalize_header_label(value: str) -> str:
    """Normalize a source header cell without introducing a fixed schema."""
    return re.sub(r"\s+", " ", _clean_cell(value)).strip(" _-:|")


def _native_header_layout(
    candidate: dict[str, Any], header_index: int,
) -> tuple[list[str], list[int], int, list[list[str]], list[dict[str, Any]]]:
    """Reconstruct logical columns and explicit multi-level header geometry.

    Header names and group spans come from extracted cell geometry. No known
    header vocabulary is used. ``headers`` remains a flattened compatibility
    field, while ``header_rows`` and ``header_cells`` preserve merged groups for
    a faithful frontend rendering.
    """
    rows: list[list[str]] = candidate["rows"]
    cell_rows: list[list[tuple[float, ...] | None]] = candidate.get("cell_bboxes", [])
    row_bboxes: list[tuple[float, ...] | None] = candidate.get("row_bboxes", [])
    if header_index >= len(rows):
        return [], [], header_index + 1, [], []

    header_cells = cell_rows[header_index] if header_index < len(cell_rows) else []
    header_row_bbox = row_bboxes[header_index] if header_index < len(row_bboxes) else None
    header_entries: list[dict[str, Any]] = []
    for col_index, label in enumerate(rows[header_index]):
        label = _normalize_header_label(label)
        if not label:
            continue
        bbox = header_cells[col_index] if col_index < len(header_cells) else None
        if bbox is None:
            continue
        header_entries.append({"index": col_index, "label": label, "bbox": bbox})

    split_children: dict[int, list[dict[str, Any]]] = {}
    consumed_header_rows: set[int] = set()
    if header_row_bbox is not None:
        header_band_bottom = float(header_row_bbox[3])
        for child_row_index in range(header_index + 1, min(len(rows), header_index + 4)):
            child_row_bbox = row_bboxes[child_row_index] if child_row_index < len(row_bboxes) else None
            if child_row_bbox is None or float(child_row_bbox[1]) >= header_band_bottom - 0.5:
                break
            child_cells = cell_rows[child_row_index] if child_row_index < len(cell_rows) else []
            child_entries: list[dict[str, Any]] = []
            for col_index, label in enumerate(rows[child_row_index]):
                label = _normalize_header_label(label)
                if not label or col_index >= len(child_cells) or child_cells[col_index] is None:
                    continue
                if len(label) > 32 or _is_numeric_token(label) or not re.search(r"[A-Za-zÀ-ž]", label):
                    continue
                child_entries.append({"index": col_index, "label": label, "bbox": child_cells[col_index]})

            if len(child_entries) < 2:
                continue
            for parent in header_entries:
                pbox = parent["bbox"]
                # A parent must end above the lower header band. Its physical
                # cell geometry is the source of the parent-child relationship.
                if float(pbox[3]) >= header_band_bottom - 1.0:
                    continue
                children = [
                    child for child in child_entries
                    if float(child["bbox"][0]) >= float(pbox[0]) - 1.0
                    and float(child["bbox"][2]) <= float(pbox[2]) + 1.0
                    and float(child["bbox"][1]) >= float(pbox[3]) - 1.5
                    and float(child["bbox"][3]) <= header_band_bottom + 1.5
                ]
                if len(children) >= 2:
                    split_children[int(parent["index"])] = sorted(children, key=lambda child: float(child["bbox"][0]))
                    consumed_header_rows.add(child_row_index)

    # Each logical column retains its source column index and parent/child labels.
    logical_columns: list[dict[str, Any]] = []
    for entry in header_entries:
        index = int(entry["index"])
        label = str(entry["label"])
        children = split_children.get(index)
        if children:
            for child in children:
                logical_columns.append({
                    "x": float(child["bbox"][0]),
                    "label": f"{label} / {child['label']}",
                    "source_index": int(child["index"]),
                    "parent_index": index,
                    "parent_label": label,
                    "child_label": str(child["label"]),
                    "parent_bbox": entry["bbox"],
                    "child_bbox": child["bbox"],
                })
        else:
            logical_columns.append({
                "x": float(entry["bbox"][0]),
                "label": label,
                "source_index": index,
                "parent_index": index,
                "parent_label": label,
                "child_label": None,
                "parent_bbox": entry["bbox"],
                "child_bbox": None,
            })
    logical_columns.sort(key=lambda item: item["x"])

    headers = [str(column["label"]) for column in logical_columns]
    source_indexes = [int(column["source_index"]) for column in logical_columns]
    header_depth = 2 if split_children else 1
    header_rows: list[list[str]] = [[str(column["parent_label"]) for column in logical_columns]]
    if header_depth == 2:
        header_rows.append([str(column["child_label"] or "") for column in logical_columns])

    structured_cells: list[dict[str, Any]] = []
    handled_parent_indexes: set[int] = set()
    for logical_index, column in enumerate(logical_columns):
        parent_index = int(column["parent_index"])
        if column["child_label"] is not None:
            if parent_index in handled_parent_indexes:
                continue
            grouped = [
                (child_index, child_column)
                for child_index, child_column in enumerate(logical_columns)
                if int(child_column["parent_index"]) == parent_index and child_column["child_label"] is not None
            ]
            if not grouped:
                continue
            start_column = min(child_index for child_index, _ in grouped)
            group_count = len(grouped)
            structured_cells.append({
                "row": 0,
                "column": start_column,
                "text": str(column["parent_label"]),
                "rowspan": 1,
                "colspan": group_count,
                "source_bbox": [round(float(v), 2) for v in column["parent_bbox"]],
            })
            for child_index, child_column in grouped:
                child_bbox = child_column["child_bbox"]
                structured_cells.append({
                    "row": 1,
                    "column": child_index,
                    "text": str(child_column["child_label"]),
                    "rowspan": 1,
                    "colspan": 1,
                    "source_bbox": [round(float(v), 2) for v in child_bbox] if child_bbox else None,
                })
            handled_parent_indexes.add(parent_index)
        else:
            structured_cells.append({
                "row": 0,
                "column": logical_index,
                "text": str(column["parent_label"]),
                "rowspan": header_depth,
                "colspan": 1,
                "source_bbox": [round(float(v), 2) for v in column["parent_bbox"]],
            })
    structured_cells.sort(key=lambda item: (int(item["row"]), int(item["column"])))
    data_start = max({header_index, *consumed_header_rows}) + 1
    return headers, source_indexes, data_start, header_rows, structured_cells


def _extract_from_native_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], float | None]:
    extracted: list[dict[str, Any]] = []
    latest_data_bottom: float | None = None

    for candidate in candidates:
        rows: list[list[str]] = candidate["rows"]
        header_index = _find_header_index(rows)
        if header_index is None:
            data_start = 0
            headers: list[str] = []
            source_indexes: list[int] = []
            header_rows: list[list[str]] = []
            header_cells: list[dict[str, Any]] = []
            header_source = "not_detected_on_page"
            logical_column_count = max(int(candidate["column_count"] or 0), max((len(row) for row in rows), default=0))
        else:
            headers, source_indexes, data_start, header_rows, header_cells = _native_header_layout(candidate, header_index)
            header_source = "detected_in_table_rows"
            if not headers:
                headers = rows[header_index]
                source_indexes = list(range(len(headers)))
                header_rows = [headers]
                header_cells = [
                    {"row": 0, "column": index, "text": str(label), "rowspan": 1, "colspan": 1, "source_bbox": None}
                    for index, label in enumerate(headers)
                ]
                data_start = header_index + 1
            logical_column_count = len(headers)

        if not logical_column_count:
            continue
        threshold = _row_population_threshold(logical_column_count)
        item_rows: list[list[str]] = []
        item_bboxes: list[tuple[float, ...]] = []
        item_bbox_records: list[list[float] | None] = []
        context_rows: list[dict[str, Any]] = []
        source_boxes: list[tuple[float, ...]] = []
        header_bbox_records = [
            box for box in candidate.get("row_bboxes", [])[header_index:data_start]
            if box is not None
        ] if header_index is not None else []
        header_source_bbox = None
        header_source_bboxes: list[list[float]] = [
            [round(float(value), 2) for value in box]
            for box in header_bbox_records
        ]
        if header_bbox_records:
            header_source_bbox = [
                round(min(box[0] for box in header_bbox_records), 2),
                round(min(box[1] for box in header_bbox_records), 2),
                round(max(box[2] for box in header_bbox_records), 2),
                round(max(box[3] for box in header_bbox_records), 2),
            ]
            source_boxes.extend(header_bbox_records)
        elif header_source_bbox is not None:
            header_source_bboxes = [header_source_bbox]
        if header_index is not None and header_index < len(candidate.get("cell_bboxes", [])):
            for cell_index in source_indexes:
                cell_row = candidate["cell_bboxes"][header_index]
                if cell_index < len(cell_row) and cell_row[cell_index] is not None:
                    source_boxes.append(cell_row[cell_index])

        for index in range(data_start, len(rows)):
            raw_row = rows[index]
            if source_indexes:
                row = [raw_row[cell_index] if cell_index < len(raw_row) else "" for cell_index in source_indexes]
            else:
                row = raw_row
            row_bbox = candidate["row_bboxes"][index] if index < len(candidate["row_bboxes"]) else None
            normalized_row = [_normalize_header_label(cell).casefold() for cell in raw_row]
            normalized_header_rows = {
                tuple(_normalize_header_label(cell).casefold() for cell in header_row)
                for header_row in (candidate["rows"][header_index:header_index + 1] if header_index is not None else [])
            }
            if normalized_row and tuple(normalized_row) in normalized_header_rows:
                context_rows.append({
                    "type": "repeated_header",
                    "cells": raw_row,
                    "text": " ".join(cell for cell in raw_row if cell).strip(),
                    "source_bbox": [round(float(value), 2) for value in row_bbox] if row_bbox else None,
                })
                continue

            # PyMuPDF may shift a populated cell into an otherwise empty source
            # column when a neighbouring cell is absent. Recover it only when
            # its horizontal geometry substantially overlaps a detected header
            # cell whose corresponding value is empty.
            if source_indexes and row_bbox and index < len(candidate.get("cell_bboxes", [])):
                row_cells = candidate["cell_bboxes"][index]
                source_header_cells = candidate.get("cell_bboxes", [])[header_index] if header_index is not None and header_index < len(candidate.get("cell_bboxes", [])) else []
                used_source_indexes = set(source_indexes)
                for source_cell_index, source_text in enumerate(raw_row):
                    if source_cell_index in used_source_indexes or not _clean_cell(source_text).strip():
                        continue
                    source_box = row_cells[source_cell_index] if source_cell_index < len(row_cells) else None
                    if source_box is None:
                        continue
                    best_target: tuple[float, int] | None = None
                    for output_index, header_source_index in enumerate(source_indexes):
                        if output_index >= len(row) or row[output_index]:
                            continue
                        header_box = source_header_cells[header_source_index] if header_source_index < len(source_header_cells) else None
                        if header_box is None:
                            continue
                        overlap = max(0.0, min(float(source_box[2]), float(header_box[2])) - max(float(source_box[0]), float(header_box[0])))
                        denominator = max(1.0, min(float(source_box[2]) - float(source_box[0]), float(header_box[2]) - float(header_box[0])))
                        ratio = overlap / denominator
                        if ratio >= 0.72 and (best_target is None or ratio > best_target[0]):
                            best_target = (ratio, output_index)
                    if best_target is not None:
                        row[best_target[1]] = _clean_cell(source_text)
            populated = [cell for cell in row if cell and cell.strip()]
            if len(populated) < threshold:
                if row_bbox and any(re.search(r"[A-Za-zÀ-ž]", cell) for cell in populated):
                    context_rows.append({
                        "type": "possible_section_heading" if len(populated) == 1 else "unclassified_context",
                        "cells": raw_row,
                        "text": " ".join(cell for cell in raw_row if cell).strip(),
                        "source_bbox": [round(float(value), 2) for value in row_bbox],
                    })
                continue
            if row_bbox:
                row_height = max(0.0, row_bbox[3] - row_bbox[1])
                # A merged cell spanning several summary/footer lines is not a
                # normal item row, even if it contains a few numeric values.
                if row_height > max(45.0, page_height_from_bbox(candidate["bbox"]) * 0.14):
                    continue
                item_bboxes.append(row_bbox)
                item_bbox_records.append([round(float(value), 2) for value in row_bbox])
                source_boxes.append(row_bbox)
            else:
                item_bbox_records.append(None)
            item_rows.append(row)

        if not item_rows:
            continue

        bbox = candidate["bbox"]
        data_bottom = max((row_bbox[3] for row_bbox in item_bboxes), default=(bbox[3] if len(bbox) == 4 else None))
        if data_bottom is not None:
            latest_data_bottom = max(latest_data_bottom or data_bottom, data_bottom)
        max_row_count = max((len(row) for row in item_rows), default=0)
        column_count = max(logical_column_count, max_row_count)
        if source_boxes:
            table_source_bbox = [
                round(min(box[0] for box in source_boxes), 2),
                round(min(box[1] for box in source_boxes), 2),
                round(max(box[2] for box in source_boxes), 2),
                round(max(box[3] for box in source_boxes), 2),
            ]
        else:
            table_source_bbox = [round(value, 2) for value in bbox] if len(bbox) == 4 else None
        table_payload = {
            "table_index": len(extracted) + 1,
            "table_role": "line_items",
            "layout_type": "item_table",
            "is_source_table": True,
            "headers": headers,
            "header_rows": header_rows,
            "header_cells": header_cells,
            "header_detected": bool(headers),
            "header_source": header_source,
            "column_count": column_count,
            "row_count": len(item_rows),
            "rows": item_rows,
            "source_bbox": table_source_bbox,
            "header_source_bbox": header_source_bbox,
            "header_source_bboxes": header_source_bboxes,
            "row_source_bboxes": item_bbox_records,
            "context_rows": context_rows,
            "detection_strategy": candidate["strategy"],
            "summary_items": [],
            "metadata_items": [],
            "detection_confidence": round(min(0.97, 0.68 + len(item_rows) * 0.02 + min(logical_column_count, 12) * 0.008), 2),
        }
        # If the same ruled region contains a form-like document header before
        # the item-table header, preserve that prefix as its own table-shaped
        # block. PyMuPDF often merges invoice header cells and the line-item
        # grid into one candidate because some border lines are shared. The
        # prefix is therefore not discarded as metadata; it is reconstructed
        # from the actual cell geometry instead of being forced into the item
        # table's column schema.
        form_table = _build_document_form_table(candidate, header_index, len(extracted) + 1)
        if form_table is not None:
            extracted.append(form_table)
            table_payload["table_index"] = len(extracted) + 1
        extracted.append(table_payload)

    return extracted, latest_data_bottom


def _merge_native_row_fragments(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join adjacent native one-row fragments only when lanes and IDs agree.

    PyMuPDF can return each ruled line of a borderless/vertically spaced table
    as a separate candidate. Shared horizontal extent, column count, row shape,
    and a consecutive source identifier sequence are required before merging.
    """
    merged: list[dict[str, Any]] = []

    def identifier(table: dict[str, Any]) -> int | None:
        rows = table.get("rows") or []
        if len(rows) != 1 or not rows[0]:
            return None
        match = re.fullmatch(r"\s*(\d{1,4})[.)]?\s*", _clean_cell(rows[0][0]))
        return int(match.group(1)) if match else None

    def effective_row_width(table: dict[str, Any]) -> int:
        rows = table.get("rows") or []
        if len(rows) != 1 or not isinstance(rows[0], (list, tuple)):
            return 0
        return max(
            (index + 1 for index, value in enumerate(rows[0]) if _clean_cell(value)),
            default=0,
        )

    def same_horizontal_lanes(left: dict[str, Any], right: dict[str, Any]) -> bool:
        a, b = left.get("source_bbox"), right.get("source_bbox")
        if not a or not b or len(a) != 4 or len(b) != 4:
            return False
        aw, bw = max(1.0, float(a[2]) - float(a[0])), max(1.0, float(b[2]) - float(b[0]))
        overlap = max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0])))
        return overlap / min(aw, bw) >= 0.94 and abs(aw - bw) <= max(4.0, aw * 0.025)

    index = 0
    while index < len(tables):
        first = tables[index]
        first_id = identifier(first)
        if (
            first_id is None or first.get("detection_strategy") != "lines"
            or not first.get("headers") or int(first.get("row_count") or 0) != 1
        ):
            merged.append(first)
            index += 1
            continue

        group = [first]
        next_index = index + 1
        expected_id = first_id + 1
        while next_index < len(tables):
            candidate = tables[next_index]
            candidate_id = identifier(candidate)
            if (
                candidate.get("detection_strategy") != "lines"
                or int(candidate.get("row_count") or 0) != 1
                or candidate.get("headers")
                or effective_row_width(candidate) != int(first.get("column_count") or 0)
                or candidate_id != expected_id
                or not same_horizontal_lanes(first, candidate)
            ):
                break
            prev_bbox = (group[-1].get("row_source_bboxes") or [None])[-1]
            next_bbox = (candidate.get("row_source_bboxes") or [None])[0]
            if not prev_bbox or not next_bbox or float(next_bbox[1]) <= float(prev_bbox[1]):
                break
            group.append(candidate)
            expected_id += 1
            next_index += 1

        if len(group) == 1:
            merged.append(first)
            index += 1
            continue

        combined = dict(first)
        logical_columns = int(first.get("column_count") or len(first.get("headers", [])))
        combined["rows"] = [
            list(row[:logical_columns]) + [""] * max(0, logical_columns - len(row))
            for item in group for row in item.get("rows", [])
        ]
        combined["row_source_bboxes"] = [box for item in group for box in item.get("row_source_bboxes", [])]
        combined["row_count"] = len(combined["rows"])
        combined["source_bbox"] = [
            round(min(float(item["source_bbox"][0]) for item in group), 2),
            round(min(float(item["source_bbox"][1]) for item in group), 2),
            round(max(float(item["source_bbox"][2]) for item in group), 2),
            round(max(float(item["source_bbox"][3]) for item in group), 2),
        ]
        combined["merged_fragment_count"] = len(group)
        combined["detection_confidence"] = round(min(0.97, float(first.get("detection_confidence") or 0.0) + 0.02 * (len(group) - 1)), 2)
        merged.append(combined)
        index = next_index

    return merged


def _cluster_boundaries(values: list[float], tolerance: float = 2.5) -> list[float]:
    """Merge nearly-identical cell edges caused by PDF line-coordinate noise."""
    if not values:
        return []
    clusters: list[list[float]] = []
    for value in sorted(values):
        if clusters and abs(value - float(median(clusters[-1]))) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [float(median(cluster)) for cluster in clusters]


def _cell_area(bbox: tuple[float, ...] | list[float]) -> float:
    if len(bbox) != 4:
        return float("inf")
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def _build_document_form_table(
    candidate: dict[str, Any],
    header_index: int | None,
    table_index: int,
) -> dict[str, Any] | None:
    """Preserve a ruled, form-like prefix before the item header as a table.

    This is layout-based: it does not look for TO, DATE, PORT, invoice number,
    or any vendor-specific field. It requires multiple pre-header rows with
    positioned cell content, and uses those cell rectangles to preserve merged
    cells and rows whose column layout differs from the item table.
    """
    if candidate.get("strategy") != "lines" or header_index is None or header_index < 2:
        return None

    rows = candidate.get("rows", [])
    cell_bboxes = candidate.get("cell_bboxes", [])
    row_bboxes = candidate.get("row_bboxes", [])
    row_payloads: list[dict[str, Any]] = []
    x_edges: list[float] = []

    for row_index in range(min(header_index, len(rows))):
        raw_row = rows[row_index] or []
        geometry_row = cell_bboxes[row_index] if row_index < len(cell_bboxes) else []
        cells: list[dict[str, Any]] = []
        for column_index, raw_value in enumerate(raw_row):
            text = _clean_cell(raw_value)
            if not text:
                continue
            bbox = geometry_row[column_index] if column_index < len(geometry_row) else None
            if not bbox or len(bbox) != 4:
                continue
            clean_bbox = tuple(float(value) for value in bbox)
            cells.append({"text": text, "bbox": clean_bbox})

        # PyMuPDF can return the same merged-cell text in both the real cell
        # and a broad overlapping cell from the combined form/item grid. Keep
        # the tighter geometry for duplicate, overlapping text; retain equal
        # text in genuinely separate cells when their boxes do not overlap.
        retained: list[dict[str, Any]] = []
        for cell in sorted(cells, key=lambda item: _cell_area(item["bbox"])):
            duplicate_index = None
            for index, previous in enumerate(retained):
                if _clean_cell(previous["text"]).casefold() != _clean_cell(cell["text"]).casefold():
                    continue
                if _bbox_overlap_ratio(previous["bbox"], cell["bbox"]) >= 0.85:
                    duplicate_index = index
                    break
            if duplicate_index is None:
                retained.append(cell)
        retained.sort(key=lambda item: (item["bbox"][0], item["bbox"][1]))
        if not retained:
            continue

        source_row_bbox = row_bboxes[row_index] if row_index < len(row_bboxes) else None
        row_bbox_list = [round(float(value), 2) for value in source_row_bbox] if source_row_bbox else None
        for cell in retained:
            x_edges.extend([float(cell["bbox"][0]), float(cell["bbox"][2])])
        row_payloads.append({"cells": retained, "source_bbox": row_bbox_list})

    # A single line of key/value text is not enough evidence to classify the
    # prefix as a separate table. Require multiple rows and multiple occupied
    # cells across at least two of those rows.
    multi_cell_rows = sum(len(row["cells"]) >= 2 for row in row_payloads)
    if len(row_payloads) < 2 or multi_cell_rows < 2:
        return None

    boundaries = _cluster_boundaries(x_edges)
    if len(boundaries) < 3:
        return None
    column_count = len(boundaries) - 1

    form_rows: list[list[dict[str, Any]]] = []
    flattened_rows: list[list[str]] = []
    row_source_bboxes: list[list[float] | None] = []
    all_cells: list[dict[str, Any]] = []
    for row in row_payloads:
        mapped_cells: list[dict[str, Any]] = []
        flattened = [""] * column_count
        for cell in row["cells"]:
            bbox = cell["bbox"]
            x0, x1 = float(bbox[0]), float(bbox[2])
            start = min(range(len(boundaries)), key=lambda i: abs(boundaries[i] - x0))
            end = min(range(len(boundaries)), key=lambda i: abs(boundaries[i] - x1))
            if abs(boundaries[start] - x0) > 4.0 or abs(boundaries[end] - x1) > 4.0:
                continue
            start = max(0, min(column_count - 1, start))
            end = max(start + 1, min(column_count, end))
            payload = {
                "column": start,
                "colspan": end - start,
                "text": cell["text"],
                "source_bbox": [round(float(value), 2) for value in bbox],
            }
            mapped_cells.append(payload)
            if not flattened[start]:
                flattened[start] = cell["text"]
            else:
                flattened[start] = f"{flattened[start]} {cell['text']}".strip()
            all_cells.append(cell)
        if not mapped_cells:
            continue
        mapped_cells.sort(key=lambda item: int(item["column"]))
        form_rows.append(mapped_cells)
        flattened_rows.append(flattened)
        row_source_bboxes.append(row["source_bbox"])

    if len(form_rows) < 2:
        return None
    source_bbox = [
        round(min(float(cell["bbox"][0]) for cell in all_cells), 2),
        round(min(float(cell["bbox"][1]) for cell in all_cells), 2),
        round(max(float(cell["bbox"][2]) for cell in all_cells), 2),
        round(max(float(cell["bbox"][3]) for cell in all_cells), 2),
    ]
    return {
        "table_index": table_index,
        "table_role": "document_header",
        "layout_type": "document_form",
        "is_source_table": True,
        "headers": [],
        "header_rows": [],
        "header_cells": [],
        "header_detected": False,
        "header_source": "not_applicable_form_layout",
        "column_count": column_count,
        "row_count": len(form_rows),
        "rows": flattened_rows,
        "form_rows": form_rows,
        "source_bbox": source_bbox,
        "header_source_bbox": None,
        "row_source_bboxes": row_source_bboxes,
        "detection_strategy": "native_ruled_form_prefix",
        "summary_items": [],
        "metadata_items": [],
        "detection_confidence": 0.82,
    }


def page_height_from_bbox(bbox: tuple[float, ...]) -> float:
    if len(bbox) != 4:
        return 0.0
    return max(0.0, bbox[3] - bbox[1])


def _visual_span_lines(page: Any) -> list[dict[str, Any]]:
    """Extract positioned spans and group them by their visual baseline."""
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        logger.exception("Unable to read positioned text spans from PDF page")
        return []
    spans: list[dict[str, Any]] = []
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = str(span.get("text", "")).strip()
                bbox = span.get("bbox")
                if not text or not bbox or len(bbox) != 4:
                    continue
                spans.append({
                    "text": text,
                    "x0": float(bbox[0]), "y0": float(bbox[1]),
                    "x1": float(bbox[2]), "y1": float(bbox[3]),
                    "y": (float(bbox[1]) + float(bbox[3])) / 2,
                })
    spans.sort(key=lambda item: (item["y"], item["x0"]))
    lines: list[dict[str, Any]] = []
    for span in spans:
        if lines and abs(span["y"] - lines[-1]["y"]) <= 2.5:
            lines[-1]["spans"].append(span)
            lines[-1]["y"] = float(median(item["y"] for item in lines[-1]["spans"]))
        else:
            lines.append({"y": span["y"], "spans": [span]})
    for line in lines:
        line["spans"].sort(key=lambda item: item["x0"])
        line["x0"] = min(item["x0"] for item in line["spans"])
        line["x1"] = max(item["x1"] for item in line["spans"])
    return lines


def _is_item_identifier(value: str) -> bool:
    """Recognize common compact row identifiers without assuming a schema."""
    value = _clean_cell(value).strip()
    if not value or len(value) > 16:
        return False
    patterns = (
        r"\(?\d+(?:[.,]\d+)?[.)]?",       # 1, 1), 1.00
        r"[A-Za-z][.)]?",                  # a, b), A.
        r"[A-Za-z]{1,4}[-_/]?\d+[A-Za-z0-9._/-]*",  # A-01, VLV100
        r"[A-Z0-9][A-Z0-9._/-]*\d[A-Z0-9._/-]*",    # compact codes with digits
    )
    return any(re.fullmatch(pattern, value) for pattern in patterns)


def _is_header_like_run(run: dict[str, Any], *, ocr_page: bool = False) -> bool:
    text = _clean_cell(run.get("text", "")).strip(" _-:|•")
    if not text or not re.search(r"[A-Za-zÀ-ž]", text):
        return False
    if _MONEY_TOKEN_RE.search(text):
        return False
    # Short labels and multiword column titles are expected; sentence-sized
    # runs are more likely prose than a table header. OCR can combine adjacent
    # header cells into one run; allow a slightly longer run only on OCR pages,
    # where observed body lanes must still prove a valid split.
    if ocr_page:
        return len(text) <= 64 and len(text.split()) <= 10
    return len(text) <= 48 and len(text.split()) <= 6


def _assign_run_to_column(run: dict[str, Any], column_starts: list[float]) -> int:
    """Assign a text run to the nearest observed header start coordinate."""
    x = float(run["x0"])
    if not column_starts:
        return 0
    return min(range(len(column_starts)), key=lambda index: abs(x - column_starts[index]))


def _is_repeated_identifier_data_line(
    line: dict[str, Any],
    lines: list[dict[str, Any]],
    page_width: float,
) -> bool:
    """Recognize a data baseline from repeated aligned identifiers and values."""
    runs = sorted(line.get("runs", []), key=lambda run: float(run.get("x0", 0)))
    if len(runs) < 3:
        return False
    first = runs[0]
    first_x = float(first["x0"])
    if first_x > page_width * 0.42 or not _is_item_identifier(str(first.get("text", ""))):
        return False

    def has_right_value(candidate_runs: list[dict[str, Any]]) -> bool:
        return any(
            _is_numeric_token(_clean_cell(run.get("text", "")))
            and float(run["x0"]) >= max(page_width * 0.54, first_x + page_width * 0.32)
            for run in candidate_runs[2:]
        )

    if not has_right_value(runs):
        return False

    following_ids = {str(first.get("text", "")).casefold()}
    peers = 0
    for other in lines:
        delta = float(other.get("y", 0)) - float(line.get("y", 0))
        if delta <= 2.5 or delta > 48.0:
            continue
        other_runs = sorted(other.get("runs", []), key=lambda run: float(run.get("x0", 0)))
        if len(other_runs) < 3:
            continue
        other_first = other_runs[0]
        if (
            abs(float(other_first["x0"]) - first_x) <= max(8.0, page_width * 0.015)
            and _is_item_identifier(str(other_first.get("text", "")))
            and has_right_value(other_runs)
        ):
            peers += 1
            following_ids.add(str(other_first.get("text", "")).casefold())
            if peers >= 2 and len(following_ids) >= 2:
                return True
    return False


def _estimate_ocr_column_x_slope(
    lines: list[dict[str, Any]],
    page_width: float,
) -> float:
    """Estimate horizontal drift of a scanned table from repeated row IDs."""
    anchors = []
    for line in lines:
        if _is_repeated_identifier_data_line(line, lines, page_width):
            runs = sorted(line.get("runs", []), key=lambda run: float(run.get("x0", 0)))
            anchors.append((float(line["y"]), float(runs[0]["x0"])))
    if len(anchors) < 4:
        return 0.0

    slopes = []
    for index, (y0, x0) in enumerate(anchors):
        for y1, x1 in anchors[index + 1:]:
            dy = y1 - y0
            if dy < 30.0:
                continue
            slope = (x1 - x0) / dy
            if abs(slope) <= 0.20:
                slopes.append(slope)
    if not slopes:
        return 0.0
    slope = float(median(slopes))
    return slope if abs(slope) >= 0.008 else 0.0


def _shift_line_x(line: dict[str, Any], shift: float) -> dict[str, Any]:
    """Return a shallow line copy shifted into the header's horizontal frame."""
    if abs(shift) < 0.01:
        return line
    shifted = dict(line)
    shifted_runs = []
    for run in line.get("runs", []):
        shifted_run = dict(run)
        shifted_run["x0"] = float(run["x0"]) - shift
        shifted_run["x1"] = float(run["x1"]) - shift
        shifted_run["words"] = []
        for word in run.get("words", []):
            shifted_word = dict(word)
            shifted_word["x0"] = float(word["x0"]) - shift
            shifted_word["x1"] = float(word["x1"]) - shift
            shifted_run["words"].append(shifted_word)
        shifted_runs.append(shifted_run)
    shifted["runs"] = shifted_runs
    shifted["words"] = [
        {**word, "x0": float(word["x0"]) - shift, "x1": float(word["x1"]) - shift}
        for word in line.get("words", [])
    ]
    shifted["x0"] = float(line["x0"]) - shift
    shifted["x1"] = float(line["x1"]) - shift
    return shifted


def _candidate_header_records(
    lines: list[dict[str, Any]],
    page_width: float,
    *,
    ocr_page: bool = False,
) -> list[dict[str, Any]]:
    """Find plausible, distributed header lines from positioned text runs."""
    candidates: list[dict[str, Any]] = []
    for line in lines:
        if ocr_page and _is_repeated_identifier_data_line(line, lines, page_width):
            continue
        runs = [
            run for run in line["runs"]
            if _is_header_like_run(run, ocr_page=ocr_page)
        ]
        if not 2 <= len(runs) <= 12:
            continue
        x0 = min(float(run["x0"]) for run in runs)
        x1 = max(float(run["x1"]) for run in runs)
        coverage = (x1 - x0) / max(1.0, page_width)
        # Do not require the visible header text to fill a large share of the
        # page width. Compact tables often have a wide empty final cell, so
        # their labels may occupy only a narrow band. Actual row-anchor evidence
        # below is responsible for distinguishing these from ordinary metadata.
        if coverage < 0.18:
            continue
        # A header is a distributed band of labels, not a sentence fragmented
        # into many parts. Several independent short runs plus broad coverage
        # give useful evidence without depending on particular header words.
        chars = sum(len(_clean_cell(run["text"])) for run in runs)
        if chars > 150:
            continue
        centers = [(float(run["x0"]) + float(run["x1"])) / 2 for run in runs]
        if any(centers[index + 1] - centers[index] < 8 for index in range(len(centers) - 1)):
            continue
        candidates.append({
            "y": float(line["y"]),
            "runs": sorted(runs, key=lambda run: float(run["x0"])),
            "x0": x0,
            "x1": x1,
            "coverage": coverage,
        })
    return candidates


def _split_stacked_header_run_by_columns(
    run: dict[str, Any], column_starts: list[float],
) -> list[dict[str, Any]]:
    """Split an OCR stacked-header run when its words align to adjacent columns."""
    words = sorted(
        (word for word in run.get("words", []) if str(word.get("text", "")).strip()),
        key=lambda word: float(word.get("x0", 0.0)),
    )
    if len(words) < 2 or len(column_starts) < 2:
        return [run]
    if any(len(str(word.get("text", ""))) > 24 for word in words):
        return [run]

    assignments = [_assign_run_to_column(word, column_starts) for word in words]
    mapped = sorted(set(assignments))
    if len(mapped) < 2 or any(right - left > 1 for left, right in zip(mapped, mapped[1:])):
        return [run]

    groups: list[list[dict[str, Any]]] = []
    group_columns: list[int] = []
    for word, column in zip(words, assignments):
        if not groups or column != group_columns[-1]:
            groups.append([])
            group_columns.append(column)
        groups[-1].append(word)
    if len(groups) < 2:
        return [run]
    return [_positioned_word_group(group) for group in groups]


def _merge_stacked_header_labels(
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    *,
    page_width: float,
    ocr_page: bool = False,
) -> tuple[list[str], list[float], list[float]]:
    """Attach nearby second-line header fragments using observed x positions."""
    runs = header["runs"]
    centers = [(float(run["x0"]) + float(run["x1"])) / 2 for run in runs]
    starts = [float(run["x0"]) for run in runs]
    headers = [_clean_cell(run["text"]).strip(" _-:") for run in runs]
    stacked_header_bboxes: list[list[float]] = []
    # Both top-stacked labels (e.g. Order / quantity) and bottom-stacked units
    # (e.g. Unit Price / (YEN)) are supported. Nearby prose is excluded by the
    # narrow vertical band and short-fragment checks.
    for line in lines:
        if ocr_page and _is_repeated_identifier_data_line(
            line, lines, page_width
        ):
            continue
        delta = float(line["y"]) - float(header["y"])
        if delta == 0 or delta < -14.0 or delta > 15.0 or abs(delta) < 2.5:
            continue
        for source_run in line["runs"]:
            stacked_runs = (
                _split_stacked_header_run_by_columns(source_run, starts)
                if ocr_page else [source_run]
            )
            for run in stacked_runs:
                raw_text = _clean_cell(run["text"])
                # Key/value metadata such as "Dock:" or "ETA:" above a quotation
                # table is not a stacked column header.
                if delta < 0 and raw_text.rstrip().endswith(":"):
                    continue
                text = raw_text.strip(" _-:")
                if not text or len(text) > 24 or not re.search(r"[A-Za-zÀ-ž]", text):
                    continue
                if _is_numeric_token(text):
                    continue
                # Decorative rules/underscores have no alphabetic content after
                # normalization and were filtered above.
                column = _assign_run_to_column(run, starts)
                # A short, numbered label immediately below a first-column header
                # is often a section/category label rather than a second header
                # row. Its position and token shape distinguish it without a
                # document-specific vocabulary.
                if (
                    not ocr_page
                    and delta > 0
                    and column == 0
                    and len(line.get("runs", [])) == 1
                    and re.match(r"^\d+(?:[.)])?\s+[A-Za-zÀ-ž]", text)
                    and float(run["x0"]) <= starts[0] + 8.0
                ):
                    continue
                center = (float(run["x0"]) + float(run["x1"])) / 2
                # Require spatial alignment with the actual header start/center.
                if abs(float(run["x0"]) - starts[column]) > 28.0 and abs(center - centers[column]) > 32.0:
                    continue
                normalized = text.strip("() ")
                if not normalized or normalized.casefold() in headers[column].casefold():
                    continue
                if delta < 0:
                    headers[column] = f"{normalized} {headers[column]}".strip()
                else:
                    headers[column] = f"{headers[column]} ({normalized})".strip()
                # Save the exact position of each attached sub-header. Metadata
                # exclusion uses these component boxes instead of expanding one big
                # header rectangle into nearby but unrelated fields.
                stacked_header_bboxes.append([
                    round(float(run["x0"]), 2),
                    round(float(run["y0"]), 2),
                    round(float(run["x1"]), 2),
                    round(float(run["y1"]), 2),
                ])
    header["stacked_header_bboxes"] = stacked_header_bboxes
    return headers, centers, starts


def _line_column_cells(
    line: dict[str, Any], column_starts: list[float], column_count: int,
    column_boundaries: list[float] | None = None,
) -> list[list[dict[str, Any]]]:
    cells: list[list[dict[str, Any]]] = [[] for _ in range(column_count)]
    for run in line["runs"]:
        pieces = _split_run_at_column_boundaries(run, column_boundaries or [])
        for piece, column in pieces:
            if column is None:
                column = _assign_run_to_geometry_column(
                    piece, column_starts, column_boundaries or []
                )
            if column < column_count:
                cells[column].append(piece)
    return cells


def _split_run_at_column_boundaries(
    run: dict[str, Any], boundaries: list[float],
    *, collapse_overlapping_duplicates: bool = False,
    split_crossing_boxes: bool = False,
    split_crossing_runs_by_word_start: bool = False,
    column_starts: list[float] | None = None,
) -> list[tuple[dict[str, Any], int | None]]:
    """Split a grouped run only where complete word boxes occupy distinct lanes."""
    words = sorted(
        (word for word in run.get("words", []) if str(word.get("text", "")).strip()),
        key=lambda word: float(word.get("x0", 0.0)),
    )
    duplicate_removed = False
    if collapse_overlapping_duplicates and len(words) > 1:
        remove_indices: set[int] = set()
        for small_index, small in enumerate(words):
            small_text = re.sub(r"[^a-z0-9]", "", _clean_cell(small.get("text", "")).casefold())
            if not small_text:
                continue
            small_box = tuple(float(small[key]) for key in ("x0", "y0", "x1", "y1"))
            small_area = max(0.0, small_box[2] - small_box[0]) * max(0.0, small_box[3] - small_box[1])
            if small_area <= 0:
                continue
            for large_index, large in enumerate(words):
                if small_index == large_index or large_index in remove_indices:
                    continue
                large_text = re.sub(r"[^a-z0-9]", "", _clean_cell(large.get("text", "")).casefold())
                if not large_text or small_text == large_text:
                    if small_index >= large_index:
                        continue
                elif len(small_text) > max(1, len(large_text) // 2) or small_text not in large_text:
                    continue
                large_box = tuple(float(large[key]) for key in ("x0", "y0", "x1", "y1"))
                intersection = (
                    max(0.0, min(small_box[2], large_box[2]) - max(small_box[0], large_box[0]))
                    * max(0.0, min(small_box[3], large_box[3]) - max(small_box[1], large_box[1]))
                )
                if intersection / small_area >= 0.70:
                    remove_indices.add(small_index)
                    duplicate_removed = True
                    break
        if remove_indices:
            words = [word for index, word in enumerate(words) if index not in remove_indices]

    def unchanged_run() -> dict[str, Any]:
        if not duplicate_removed:
            return run
        cleaned = _positioned_word_group(words)
        cleaned["overlapping_duplicate_words_removed"] = True
        return cleaned

    if len(words) < 2 or not boundaries:
        return [(unchanged_run(), None)]

    assignments: list[int] = []
    crossing_word_ids: set[int] = set()
    supported_boundary_indexes: set[int] = set()
    if split_crossing_runs_by_word_start and column_starts:
        for index, boundary in enumerate(boundaries):
            has_crossing_word = any(
                float(word["x0"]) < boundary < float(word["x1"])
                for word in words
            )
            has_word_at_next_lane = (
                index + 1 < len(column_starts)
                and any(
                float(candidate["x0"]) >= boundary
                and abs(float(candidate["x0"]) - float(column_starts[index + 1])) <= 22.0
                for candidate in words
            )
            )
            if has_crossing_word and has_word_at_next_lane:
                supported_boundary_indexes.add(index)
    if supported_boundary_indexes and not split_crossing_boxes:
        # Honor only separators for which the run contains a word starting at
        # the next header lane. Long descriptions can cross geometric midpoints
        # without entering the adjacent value column; those unsupported
        # midpoints must not split the description.
        current_column = bisect_right(boundaries, float(words[0]["x0"]))
        for word in words:
            x0 = float(word["x0"])
            for index in sorted(supported_boundary_indexes):
                if x0 >= boundaries[index]:
                    current_column = max(current_column, index + 1)
            assignments.append(current_column)
    else:
        for word in words:
            x0, x1 = float(word["x0"]), float(word["x1"])
            if any(x0 < boundary < x1 for boundary in boundaries):
                if not split_crossing_boxes:
                    return [(unchanged_run(), None)]
                # OCR can return one oversized box crossing several lanes while
                # other words on that baseline have clear positions. Keep the wide
                # token in the lane where its text starts and flag that cell; do not
                # let it collapse the separable neighboring words into one column.
                assignments.append(bisect_right(boundaries, x0))
                crossing_word_ids.add(id(word))
            else:
                assignments.append(bisect_right(boundaries, (x0 + x1) / 2.0))
    if len(set(assignments)) < 2:
        unchanged = dict(unchanged_run())
        if crossing_word_ids:
            unchanged["crossing_column_boundary"] = True
        return [(unchanged, None)]

    pieces: list[tuple[dict[str, Any], int | None]] = []
    group: list[dict[str, Any]] = []
    group_column = assignments[0]

    def append_group(group_words: list[dict[str, Any]], column_index: int) -> None:
        piece = _positioned_word_group(group_words)
        if any(id(word) in crossing_word_ids for word in group_words):
            piece["crossing_column_boundary"] = True
        pieces.append((piece, column_index))

    for word, column in zip(words, assignments):
        if group and column != group_column:
            append_group(group, group_column)
            group = []
        if not group:
            group_column = column
        group.append(word)
    if group:
        append_group(group, group_column)
    if duplicate_removed and pieces:
        pieces[0][0]["overlapping_duplicate_words_removed"] = True
    return pieces


def _column_boundaries_from_observed_lanes(lanes: list[float]) -> list[float]:
    """Use observed starts to keep long text inside unusually wide columns.

    For a gap that is much wider than the other lane gaps, the right lane start
    is a better separator than the midpoint: wrapped text in the wide cell can
    extend almost to that start. The small inset leaves room for OCR box jitter.
    """
    gaps = [right - left for left, right in zip(lanes, lanes[1:])]
    positive_gaps = [gap for gap in gaps if gap > 0]
    if not positive_gaps:
        return []
    typical_gap = float(median(positive_gaps))
    boundaries: list[float] = []
    for left, right in zip(lanes, lanes[1:]):
        gap = right - left
        if gap > typical_gap * 2.5:
            inset = max(2.0, min(6.0, typical_gap * 0.1))
            boundaries.append(right - inset)
        else:
            boundaries.append((left + right) / 2.0)
    return boundaries


def _is_wide_observed_text_lane(column: int, lanes: list[float]) -> bool:
    """Whether a non-identifier lane has unusually wide source geometry."""
    gaps = [right - left for left, right in zip(lanes, lanes[1:])]
    positive_gaps = [gap for gap in gaps if gap > 0]
    if column <= 0 or column >= len(lanes) - 1 or not positive_gaps:
        return False
    return gaps[column] > float(median(positive_gaps)) * 2.5


def _positioned_word_group(words: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "text": " ".join(str(word["text"]).strip() for word in words),
        "x0": min(float(word["x0"]) for word in words),
        "y0": min(float(word["y0"]) for word in words),
        "x1": max(float(word["x1"]) for word in words),
        "y1": max(float(word["y1"]) for word in words),
        "words": words,
    }


def _column_boundaries_from_header(header_runs: list[dict[str, Any]]) -> list[float]:
    boundaries: list[float] = []
    for left, right in zip(header_runs, header_runs[1:]):
        left_edge = float(left["x1"])
        right_edge = float(right["x0"])
        if left_edge <= right_edge:
            boundaries.append((left_edge + right_edge) / 2.0)
        else:
            left_center = (float(left["x0"]) + left_edge) / 2.0
            right_center = (right_edge + float(right["x1"])) / 2.0
            boundaries.append((left_center + right_center) / 2.0)
    return boundaries


def _assign_run_to_geometry_column(
    run: dict[str, Any], column_starts: list[float], boundaries: list[float],
) -> int:
    x0, x1 = float(run["x0"]), float(run["x1"])
    if boundaries and not any(x0 < boundary < x1 for boundary in boundaries):
        return bisect_right(boundaries, (x0 + x1) / 2.0)
    return _assign_run_to_column(run, column_starts)




def _assign_continuation_column(
    run: dict[str, Any],
    header_runs: list[dict[str, Any]],
    column_starts: list[float],
    previous_column: int | None,
    previous_run: dict[str, Any] | None,
) -> int | None:
    """Map a wrapped text fragment to a table column without fixed schemas.

    Continuation fragments may begin slightly left of a wide column's header or
    continue to the right on the same visual line. Use horizontal overlap with
    observed header-label spans first, then close same-line continuation, and
    only then the stricter nearest-start alignment.
    """
    x0, x1 = float(run["x0"]), float(run["x1"])
    overlaps: list[tuple[float, int]] = []
    for index, header_run in enumerate(header_runs[:len(column_starts)]):
        overlap = max(0.0, min(x1, float(header_run["x1"])) - max(x0, float(header_run["x0"])))
        if overlap > 0:
            overlaps.append((overlap, index))
    if overlaps:
        return max(overlaps, key=lambda item: item[0])[1]

    if previous_column is not None and previous_run is not None:
        gap = x0 - float(previous_run["x1"])
        heights = [max(1.0, float(word.get("y1", 0)) - float(word.get("y0", 0)))
                   for word in run.get("words", [])]
        typical_height = float(median(heights)) if heights else 10.0
        if -2.0 <= gap <= max(30.0, typical_height * 3.8):
            return previous_column

    column = _assign_run_to_column(run, column_starts)
    start_distance = abs(x0 - float(column_starts[column]))
    if start_distance <= 12.0:
        return column
    return None

def _split_header_runs_by_observed_lanes(
    header: dict[str, Any], lines: list[dict[str, Any]], *, ocr_page: bool = False,
) -> dict[str, Any]:
    """Split accidentally merged header words when data proves two columns.

    PDF text extraction can join adjacent header cells when their visual gap is
    small. We split a header run only when its constituent word positions align
    with distinct, repeatedly observed data lanes below the header.
    """
    header_y = float(header["y"])
    header_runs = header["runs"]
    if len(header_runs) < 2:
        return header
    first_x = float(header_runs[0]["x0"])
    first_column_gap = (
        max(0.0, float(header_runs[1]["x0"]) - first_x)
        if len(header_runs) > 1 else 0.0
    )
    # First-column values are frequently indented within the header cell
    # (e.g. item number 1 under "Item No"). Scale the alignment tolerance to
    # the observed first-column width rather than demanding identical x starts.
    first_anchor_tolerance = max(18.0, min(48.0, first_column_gap * 0.55))
    likely_data_starts: list[tuple[float, int]] = []
    likely_data_row_count = 0
    observed_page_width = max(
        (float(line.get("x1", 0.0)) for line in lines), default=600.0
    )
    for line in lines:
        y = float(line["y"])
        if y <= header_y + 13.0 or y > header_y + 280.0:
            continue
        if not line["runs"]:
            continue
        first = line["runs"][0]
        if abs(float(first["x0"]) - first_x) > first_anchor_tolerance or not _is_item_identifier(first["text"]):
            continue
        if sum(bool(run.get("text", "").strip()) for run in line["runs"]) < 2:
            continue
        likely_data_row_count += 1
        # Keep the identifier lane as evidence too. When the first OCR header
        # run contains both an identifier label and the adjacent description
        # label, excluding this first data run leaves the identifier word with
        # no observed lane to align to, so the header cannot be split reliably.
        likely_data_starts.append((float(first["x0"]), likely_data_row_count - 1))
        description_start = None
        for run_index, run in enumerate(line["runs"][1:], start=1):
            words = list(run.get("words", []))
            run_x = float(run["x0"])
            if description_start is None:
                # The first run after an item identifier anchors the prose
                # lane. Later word runs inside that wide cell (catalog refs,
                # wrapped phrases, etc.) are content, not extra columns.
                description_start = run_x
                likely_data_starts.append((run_x, likely_data_row_count - 1))
                continue
            # Adjacent data columns can touch visually (e.g. quantity followed
            # by a unit token). Additional lanes need independent compact or
            # numeric evidence and must be separated from the prose start;
            # repeated word starts inside a long description are ignored.
            if len(words) == 2 and any(_is_numeric_token(str(word.get("text", ""))) for word in words):
                likely_data_starts.extend(
                    (float(word["x0"]), likely_data_row_count - 1)
                    for word in words
                )
                continue
            run_text = _clean_cell(run.get("text", "")).strip()
            compact_cell = (
                len(words) <= 2
                and len(run_text) <= 10
                and bool(re.fullmatch(r"[A-Za-z0-9./%+_-]+", run_text))
            )
            numeric_cell = _is_numeric_token(run_text)
            if (
                (compact_cell or numeric_cell)
                and run_x - description_start >= observed_page_width * 0.10
            ):
                likely_data_starts.append((run_x, likely_data_row_count - 1))
    # One isolated row can contain a currency marker and amount as separate
    # visual runs inside one wide source cell. It is not enough evidence to
    # infer columns from body positions alone. A merged Qty/Unit header is a
    # narrow exception: its two semantic labels plus the aligned source row
    # provide independent evidence for both observed lanes.
    normalized_header_text = " ".join(
        re.sub(
            r"[^a-z0-9]+", " ",
            re.sub(
                r"(?<=[a-z])['’](?=[a-z])", "",
                str(run.get("text", "")).casefold(),
            ),
        ).strip()
        for run in header_runs
    )
    header_tokens = set(normalized_header_text.split())
    has_quantity_unit_header = bool(
        header_tokens.intersection({"qty", "quantity"})
        and header_tokens.intersection({"unit", "uom"})
    )
    if (
        not likely_data_starts
        or likely_data_row_count < 1
        or (likely_data_row_count < 2 and not has_quantity_unit_header)
    ):
        return header

    # Collapse starts repeated across item rows into lanes. The tolerance is
    # geometric (PDF points), not tied to any header or vendor name.
    clusters: list[list[tuple[float, int]]] = []
    for x, row_index in sorted(likely_data_starts):
        if clusters and abs(x - median(item[0] for item in clusters[-1])) <= 8.0:
            clusters[-1].append((x, row_index))
        else:
            clusters.append([(x, row_index)])
    minimum_lane_support = (
        1 if likely_data_row_count < 2 and has_quantity_unit_header
        else max(2, math.ceil(likely_data_row_count * 0.25))
    )
    lanes = [
        float(median(x for x, _row in cluster))
        for cluster in clusters
        if len({row_index for _x, row_index in cluster}) >= minimum_lane_support
    ]
    # OCR can repeatedly start words from a long description at a similar x
    # (for example, a catalog reference embedded mid-sentence), making that
    # text position look like an extra column lane. A genuine lane in a compact
    # group of value columns is supported by much smaller neighboring gaps.
    # Remove only an interior lane bracketed by two unusually wide gaps; close
    # neighboring header labels remain available for the split logic below.
    header_words_span_lanes = False
    if lanes:
        for run in header_runs:
            if len(run.get("words", [])) < 2:
                continue
            word_positions = [
                float(word["x0"])
                for word in run.get("words", [])
                if str(word.get("text", "")).strip() and word.get("x0") is not None
            ]
            if not word_positions:
                continue
            mapped_header_lanes = {
                min(range(len(lanes)), key=lambda lane_index: abs(x - lanes[lane_index]))
                for x in word_positions
            }
            if len(mapped_header_lanes) > 1:
                header_words_span_lanes = True
                break
    while (
        len(lanes) > len(header_runs)
        and len(lanes) >= 4
        and not header_words_span_lanes
    ):
        gaps = [right - left for left, right in zip(lanes, lanes[1:])]
        typical_gap = float(median(sorted(gaps)[:max(2, len(gaps) // 2)]))
        wide_gap_threshold = max(24.0, typical_gap * 2.5)
        redundant_indexes = [
            index for index in range(1, len(lanes) - 1)
            if min(gaps[index - 1], gaps[index]) >= wide_gap_threshold
        ]
        if not redundant_indexes:
            break
        drop_index = max(
            redundant_indexes,
            key=lambda index: min(gaps[index - 1], gaps[index]),
        )
        lanes.pop(drop_index)
    if len(lanes) < 2:
        return header
    header = dict(header)
    header["observed_data_lanes"] = lanes

    rebuilt: list[dict[str, Any]] = []
    changed = False
    for run in header_runs:
        words = list(run.get("words", []))
        if len(words) < 2:
            rebuilt.append(run)
            continue
        assignments: list[int | None] = []
        for word in words:
            x = float(word["x0"])
            lane_index = min(range(len(lanes)), key=lambda index: abs(x - lanes[index]))
            assignments.append(lane_index if abs(x - lanes[lane_index]) <= 22.0 else None)
        if ocr_page:
            # OCR sometimes places a second-line header fragment just inside
            # the next data lane. Reattach a compact neighboring token only
            # when its box is nearly touching the preceding word, it sits lower
            # on the page, and it would otherwise cross exactly one lane.
            for word_index in range(1, len(words)):
                previous_text = re.sub(r"[^A-Za-z]", "", str(words[word_index - 1].get("text", "")))
                current_raw = str(words[word_index].get("text", "")).strip()
                current_text = re.sub(r"[^A-Za-z]", "", current_raw)
                word_gap = float(words[word_index]["x0"]) - float(words[word_index - 1]["x1"])
                previous_center_y = (
                    float(words[word_index - 1]["y0"]) + float(words[word_index - 1]["y1"])
                ) / 2.0
                current_center_y = (
                    float(words[word_index]["y0"]) + float(words[word_index]["y1"])
                ) / 2.0
                previous_height = max(
                    1.0, float(words[word_index - 1]["y1"]) - float(words[word_index - 1]["y0"])
                )
                current_height = max(
                    1.0, float(words[word_index]["y1"]) - float(words[word_index]["y0"])
                )
                lower_neighbor_is_wrapped = (
                    len(previous_text) <= 8
                    and 1 <= len(current_text) <= 8
                    and current_center_y - previous_center_y >= max(2.0, min(previous_height, current_height) * 0.30)
                )
                opening_mark_starts_wrapped_label = (
                    current_raw.startswith("(")
                    and current_center_y - previous_center_y >= max(2.0, min(previous_height, current_height) * 0.30)
                )
                compact_abbreviation_crosses_lane = (
                    len(previous_text) == 1
                    and len(current_text) == 1
                    and current_raw.endswith(".")
                    and abs(current_center_y - previous_center_y) <= max(previous_height, current_height)
                )
                if (
                    assignments[word_index] is not None
                    and assignments[word_index - 1] is not None
                    and abs(assignments[word_index] - assignments[word_index - 1]) == 1
                    and (
                        (-8.0 <= word_gap <= 10.0
                         and (lower_neighbor_is_wrapped or opening_mark_starts_wrapped_label))
                        or (-2.0 <= word_gap <= 9.0 and compact_abbreviation_crosses_lane)
                    )
                ):
                    assignments[word_index] = assignments[word_index - 1]
                elif (
                    str(words[word_index - 1].get("text", "")).strip().startswith("(")
                    and 1 <= len(current_text) <= 4
                    and assignments[word_index] is not None
                    and assignments[word_index - 1] is not None
                    and abs(assignments[word_index] - assignments[word_index - 1]) == 1
                    and -4.0 <= word_gap <= 10.0
                    and abs(current_center_y - previous_center_y) <= max(previous_height, current_height)
                ):
                    assignments[word_index] = assignments[word_index - 1]
        mapped = {value for value in assignments if value is not None}
        if len(mapped) < 2:
            rebuilt.append(run)
            continue
        # Require the split words to map to adjacent data lanes; this avoids
        # splitting normal multi-word labels where inner words merely start
        # farther to the right inside one wide cell.
        mapped_sorted = sorted(mapped)
        if any(b - a > 1 for a, b in zip(mapped_sorted, mapped_sorted[1:])):
            rebuilt.append(run)
            continue
        groups: list[dict[str, Any]] = []
        for word, lane_index in zip(words, assignments):
            if lane_index is None:
                # Attach an ambiguous word to the nearest group by x position.
                target_lane = min(mapped, key=lambda lane: abs(float(word["x0"]) - lanes[lane]))
            else:
                target_lane = lane_index
            if not groups or groups[-1]["lane"] != target_lane:
                groups.append({"lane": target_lane, "words": []})
            groups[-1]["words"].append(word)
        if len(groups) < 2:
            rebuilt.append(run)
            continue
        changed = True
        for entry in groups:
            group = entry["words"]
            rebuilt_run = _positioned_word_group(group)
            if ocr_page:
                rebuilt_run["text"] = re.sub(
                    r"\b([A-Za-z])\s+([A-Za-z]\.)",
                    r"\1\2",
                    rebuilt_run["text"],
                )
            rebuilt.append(rebuilt_run)
    if changed:
        rebuilt.sort(key=lambda run: float(run["x0"]))
        header = dict(header)
        header["runs"] = rebuilt
        header["x0"] = min(float(run["x0"]) for run in rebuilt)
        header["x1"] = max(float(run["x1"]) for run in rebuilt)
    return header


def _split_run_on_observed_lanes(run: dict[str, Any], lanes: list[float]) -> list[dict[str, Any]]:
    """Split a short PDF run only when its words align with distinct data lanes."""
    words = list(run.get("words", []))
    if len(words) < 2 or not lanes:
        return [run]
    assignments: list[int | None] = []
    for word in words:
        x = float(word["x0"])
        lane_index = min(range(len(lanes)), key=lambda index: abs(x - lanes[index]))
        assignments.append(lane_index if abs(x - lanes[lane_index]) <= 22.0 else None)
    mapped = {value for value in assignments if value is not None}
    if len(mapped) < 2:
        return [run]
    # Do not split regular descriptions into words: only split when every
    # segment is a short token and at least one token is numeric.
    if len(words) > 3 or not any(_is_numeric_token(str(word.get("text", ""))) for word in words):
        return [run]
    groups: list[dict[str, Any]] = []
    for word, lane_index in zip(words, assignments):
        if lane_index is None:
            target_lane = min(mapped, key=lambda lane: abs(float(word["x0"]) - lanes[lane]))
        else:
            target_lane = lane_index
        if not groups or groups[-1]["lane"] != target_lane:
            groups.append({"lane": target_lane, "words": []})
        groups[-1]["words"].append(word)
    if len(groups) < 2:
        return [run]
    output: list[dict[str, Any]] = []
    for entry in groups:
        group = entry["words"]
        piece = {
            "text": " ".join(str(word["text"]) for word in group).strip(),
            "x0": min(float(word["x0"]) for word in group),
            "y0": min(float(word["y0"]) for word in group),
            "x1": max(float(word["x1"]) for word in group),
            "y1": max(float(word["y1"]) for word in group),
            "words": group,
        }
        output.append(piece)
    return output

def _extract_geometry_table(
    page: Any,
    lines: list[dict[str, Any]],
    summary_candidates: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, float | None]:
    """Reconstruct an unruled table using its observed header and x geometry.

    Unlike the prior fallback, a row no longer needs three numeric tokens.
    The header anchors the column layout; compact row identifiers or repeated
    aligned cell occupancy identify data rows. The search stops at a large gap
    after a discovered row sequence, reducing the chance of absorbing later
    notes or legal text.
    """
    page_width = float(page.rect.width)
    is_ocr_page = bool(getattr(page, "ocr_word_count", 0))
    header_candidates = _candidate_header_records(
        lines, page_width, ocr_page=is_ocr_page
    )
    if not header_candidates:
        return None, None

    evaluated: list[tuple[
        float, dict[str, Any], list[dict[str, Any]], list[float], list[str],
        list[float], float, float,
    ]] = []
    # Visual baselines are y-sorted; use geometric bands to avoid rescanning
    # lines outside each header, context, or data-row region.
    line_y_positions = [float(line["y"]) for line in lines]
    ocr_x_slope = _estimate_ocr_column_x_slope(lines, page_width) if is_ocr_page else 0.0
    for original_header in header_candidates:
        header = _split_header_runs_by_observed_lanes(
            original_header, lines, ocr_page=is_ocr_page
        )
        headers, centers, column_starts = _merge_stacked_header_labels(
            header, lines, page_width=page_width, ocr_page=is_ocr_page
        )
        column_boundaries = _column_boundaries_from_header(header["runs"])
        if len(headers) < 2:
            continue
        header_y = float(header["y"])
        x_slope = ocr_x_slope
        first_col_left = float(header["runs"][0]["x0"])
        col0_right = (column_starts[0] + column_starts[1]) / 2 if len(column_starts) > 1 else float(header["runs"][0]["x1"]) + 30
        anchors: list[dict[str, Any]] = []
        header_heights = [
            max(1.0, float(run["y1"]) - float(run["y0"]))
            for run in header["runs"]
        ]
        anchor_clearance = (
            max(2.0, float(median(header_heights)) * 0.55)
            if is_ocr_page else 13.0
        )
        first_anchor_line = bisect_right(line_y_positions, header_y + anchor_clearance)
        for line in lines[first_anchor_line:]:
            y = float(line["y"])
            # Later candidate anchors beyond this same gap are discarded below
            # when the table's contiguous row sequence is formed. Stop here so
            # unrelated footer/notes text is not scanned as table geometry.
            if anchors and y - float(anchors[-1]["y"]) > 112.0:
                break
            # Stay within the page; later non-table sections are cut off by a
            # large vertical gap between row starts.
            x_shift = x_slope * (y - header_y)
            aligned_line = _shift_line_x(line, x_shift)
            aligned_column_starts = [float(start) - x_shift for start in column_starts]
            cells = _line_column_cells(
                aligned_line, aligned_column_starts, len(headers), column_boundaries
            )
            first_column_gap = (
                max(0.0, aligned_column_starts[1] - aligned_column_starts[0])
                if len(aligned_column_starts) > 1 else 0.0
            )
            first_anchor_tolerance = max(18.0, min(48.0, first_column_gap * 0.55))
            anchor_column_left = aligned_column_starts[0] if aligned_column_starts else first_col_left
            first_candidates = [
                run for run in cells[0]
                if _is_item_identifier(run["text"])
                # A compact code embedded after a label (for example
                # "Hull No. H2535") is metadata, not an item row ID. Keep
                # anchors near the observed first-column start, with tolerance
                # scaled to that column's geometry.
                and float(run["x0"]) <= anchor_column_left + first_anchor_tolerance
                and float(run["x1"]) >= anchor_column_left - 8
            ]
            if not first_candidates:
                continue
            populated_columns = sum(bool(cell) for cell in cells)
            if populated_columns < 2:
                continue
            # Avoid row-looking metadata that has an identifier but no spread
            # across the detected columns. At least one non-index cell is needed.
            other_text = [run for cell in cells[1:] for run in cell]
            if not other_text:
                continue
            anchors.append({"y": y, "line": line, "cells": cells})
        anchors.sort(key=lambda item: item["y"])
        if not anchors:
            continue

        # Drop improbable anchor jumps. A gap can be larger than a normal item
        # line because a description may wrap; allow a wide band, but do not
        # jump into a distant notes/remarks section lower on the page.
        grouped_anchors: list[dict[str, Any]] = []
        for anchor in anchors:
            if grouped_anchors:
                gap = anchor["y"] - grouped_anchors[-1]["y"]
                if gap > 112.0:
                    break
            grouped_anchors.append(anchor)
        if not grouped_anchors:
            continue

        # Rank header/data combinations: more independent item starts and a
        # wider, more distributed header are stronger evidence of a true table.
        mean_distance = median([item["y"] - header_y for item in grouped_anchors])
        # Prefer the closest preceding header band when multiple plausible bands
        # can anchor the same item rows. This helps prevent nearby document
        # metadata from winning over the actual table header on OCR pages.
        first_anchor_distance = max(0.0, float(grouped_anchors[0]["y"]) - header_y)
        header_text = " ".join(str(run.get("text", "")) for run in header["runs"])
        header_alphanumeric = re.sub(r"[^A-Za-z0-9]", "", header_text)
        header_digit_share = (
            sum(character.isdigit() for character in header_alphanumeric)
            / max(1, len(header_alphanumeric))
        )
        score = (
            len(grouped_anchors) * 20
            + len(headers) * 2
            + float(header["coverage"]) * 10
            - header_digit_share * 40
            - mean_distance / 25
            - first_anchor_distance * 0.4
        )
        if is_ocr_page:
            observed_lane_count = len(header.get("observed_data_lanes", []))
            # When repeated OCR rows expose more stable cell starts than a
            # candidate header has labels, that candidate is likely a nearby
            # metadata band (or a header that OCR merged). Prefer candidates
            # whose labels can be reconciled with those independently observed
            # lanes; a later true header is stronger evidence than a page title.
            if observed_lane_count >= 3 and len(grouped_anchors) >= 3:
                missing_header_lanes = max(0, observed_lane_count - len(headers))
                score -= min(60.0, missing_header_lanes * 12.0)
        evaluated.append((
            score, header, grouped_anchors, column_starts, headers, centers,
            header_y, x_slope,
        ))

    if not evaluated:
        return None, None
    evaluated.sort(key=lambda item: item[0], reverse=True)
    score, header, anchors, column_starts, headers, centers, header_y, x_slope = evaluated[0]

    # Convert the raw header line into a true table bbox, preserving all content
    # from each item start through the next item start only when text remains
    # aligned with the observed column starts. This captures wrapped descriptions
    # without swallowing isolated subtotal amounts into the previous row.
    header_column_starts = [float(run["x0"]) for run in header["runs"]]
    observed_data_lanes = [float(value) for value in header.get("observed_data_lanes", [])]
    observed_lane_gaps = [
        right - left
        for left, right in zip(observed_data_lanes, observed_data_lanes[1:])
    ]
    # Header labels can be visually centered far from where body text begins.
    # When repeated identifier rows provide a one-to-one set of body lanes,
    # use those observed starts for assigning row text while retaining the
    # source header geometry for its boundaries and provenance. This prevents
    # a long description run from being assigned to the identifier column just
    # because it begins left of the midpoint between widely separated labels.
    has_aligned_observed_lanes = (
        len(observed_data_lanes) == len(headers)
        and observed_lane_gaps
        and min(observed_lane_gaps) >= 9.0
        and all(left < right for left, right in zip(observed_data_lanes, observed_data_lanes[1:]))
    )
    if has_aligned_observed_lanes:
        column_starts = observed_data_lanes
    else:
        column_starts = header_column_starts
    column_boundaries = _column_boundaries_from_header(header["runs"])
    if is_ocr_page and has_aligned_observed_lanes:
        column_boundaries = _column_boundaries_from_observed_lanes(observed_data_lanes)
    row_records: list[tuple[list[str], list[float]]] = []
    geometry_row_review_reasons: list[list[str]] = []
    context_rows: list[dict[str, Any]] = []
    first_data_y = float(anchors[0]["y"])
    # Header labels can be centered far from the actual body lanes. When the
    # row anchors establish one body start per dynamic column, use those starts
    # to distinguish identifier-lane section labels from description text.
    # Otherwise a wrapped description beginning at its observed lane can be
    # mistaken for a section heading and silently omitted from the item row.
    first_lane_boundary = (
        (float(column_starts[0]) + float(column_starts[1])) / 2.0
        if has_aligned_observed_lanes and len(column_starts) > 1
        else column_boundaries[0] if column_boundaries
        else float(header["runs"][0]["x1"])
    )
    # Preserve a numbered category line between the header and first item as
    # source context, while keeping it out of the header and item cells.
    context_start = bisect_right(line_y_positions, header_y + 2.5)
    context_end = bisect_left(line_y_positions, first_data_y - 2.5)
    for line in lines[context_start:context_end]:
        y = float(line["y"])
        if not (header_y + 2.5 < y < first_data_y - 2.5) or len(line.get("runs", [])) != 1:
            continue
        run = line["runs"][0]
        text = _clean_cell(run.get("text", "")).strip()
        if (
            not is_ocr_page
            and float(run["x0"]) < first_lane_boundary
            and re.match(r"^\d+(?:[.)])?\s+[A-Za-zÀ-ž]", text)
        ):
            context_rows.append({
                "type": "possible_section_heading",
                "cells": [text],
                "text": text,
                "source_bbox": [round(float(run[key]), 2) for key in ("x0", "y0", "x1", "y1")],
            })
    item_bottom = float(anchors[-1]["line"]["y"])
    gaps = [anchors[index + 1]["y"] - anchors[index]["y"] for index in range(len(anchors) - 1)]
    typical_gap = float(median(gaps)) if gaps else 26.0
    last_row_end = anchors[-1]["y"] + min(48.0, max(4.0, typical_gap * 1.15))

    for index, anchor in enumerate(anchors):
        if is_ocr_page:
            # OCR can place a wrapped description line just above its item
            # number baseline. Divide the gap at its midpoint. Add a small
            # geometry tolerance and assign the boundary to the preceding row;
            # OCR baselines can straddle the exact midpoint by a few points
            # even when the continuation clearly belongs to that row.
            row_boundary_tolerance = 5.0
            row_start = (
                float((anchors[index - 1]["y"] + anchor["y"]) / 2.0)
                + row_boundary_tolerance
                if index > 0 else float(header_y + 2.5)
            )
            row_end = (
                float((anchor["y"] + anchors[index + 1]["y"]) / 2.0)
                + row_boundary_tolerance
                if index + 1 < len(anchors) else last_row_end
            )
        else:
            row_start = float(anchor["y"] - 2.5)
            row_end = float(anchors[index + 1]["y"] - 2.0) if index + 1 < len(anchors) else last_row_end
        cells: list[list[str]] = [[] for _ in headers]
        row_spans: list[dict[str, Any]] = []
        alignment_uncertain = False
        row_line_start = bisect_left(line_y_positions, row_start)
        row_line_end = bisect_left(line_y_positions, row_end)
        for line in lines[row_line_start:row_line_end]:
            y = float(line["y"])
            if y < row_start or y >= row_end:
                continue
            if (
                is_ocr_page
                and y > anchor["y"] + 2.5
                and len(line.get("runs", [])) == 1
            ):
                margin_run = line["runs"][0]
                margin_text = _clean_cell(margin_run.get("text", "")).strip()
                if (
                    float(margin_run["x0"]) < first_lane_boundary
                    and re.search(r"[A-Za-zÀ-ž]", margin_text)
                    and not _is_item_identifier(margin_text)
                ):
                    # Preserve scan notes/footer text in the identifier gutter
                    # as context instead of appending it to the item number.
                    context_rows.append({
                        "type": "unclassified_context",
                        "cells": [margin_text],
                        "text": margin_text,
                        "source_bbox": [
                            round(float(margin_run[key]), 2)
                            for key in ("x0", "y0", "x1", "y1")
                        ],
                    })
                    continue
            if is_ocr_page:
                line_has_identifier = any(
                    float(run["x0"]) < first_lane_boundary
                    and _is_item_identifier(str(run.get("text", "")))
                    for run in line.get("runs", [])
                )
                text_continuations = [
                    run for run in line.get("runs", [])
                    if float(run["x0"]) >= first_lane_boundary
                    and len(_clean_cell(run.get("text", "")).strip()) >= 12
                    and re.search(r"[A-Za-zÀ-ž]", str(run.get("text", "")))
                ]
                body_value_lanes = (
                    observed_data_lanes[2:]
                    if len(observed_data_lanes) == len(headers)
                    else column_starts[2:]
                )
                compact_value_evidence = sum(
                    1 for run in line.get("runs", [])
                    if len(_clean_cell(run.get("text", "")).strip()) <= 12
                    and any(
                        abs(float(run["x0"]) - float(lane)) <= 20.0
                        for lane in body_value_lanes
                    )
                )
                if (
                    not line_has_identifier
                    and text_continuations
                    and compact_value_evidence < 2
                    and len(headers) > 1
                ):
                    # A wrapped OCR description can visually extend through
                    # several empty numeric cells. Keep that text in the
                    # description lane unless separate compact values provide
                    # evidence for additional populated cells on this line.
                    cells[1].extend(
                        _clean_cell(run.get("text", ""))
                        for run in line.get("runs", [])
                        if float(run["x0"]) >= first_lane_boundary
                        and _clean_cell(run.get("text", "")).strip()
                    )
                    row_spans.extend(line.get("runs", []))
                    continue
            if index == len(anchors) - 1 and y > anchor["y"] + 2.5 and summary_candidates:
                is_positioned_summary = any(
                    abs(float(summary.get("_y", -10000.0)) - y) <= 3.0
                    and summary.get("source_bbox")
                    for summary in summary_candidates
                )
                if is_positioned_summary:
                    # Keep the total as a separate summary value instead of
                    # merging its label into the preceding invoice/item row.
                    continue
            # Section labels commonly begin in the margin/identifier lane and
            # span only that lane plus whitespace. Description continuations
            # start at the description lane and remain part of the item row.
            if y > anchor["y"] + 2.5 and len(line.get("runs", [])) == 1:
                context_run = line["runs"][0]
                context_text = _clean_cell(context_run.get("text", "")).strip()
                if (
                    not is_ocr_page
                    and float(context_run["x0"]) < first_lane_boundary
                    and re.search(r"[A-Za-zÀ-ž]", context_text)
                    and not _is_item_identifier(context_text)
                ):
                    context_rows.append({
                        "type": "possible_section_heading",
                        "cells": [context_text],
                        "text": context_text,
                        "source_bbox": [round(float(context_run[key]), 2) for key in ("x0", "y0", "x1", "y1")],
                    })
                    continue
            previous_continuation_column: int | None = None
            previous_continuation_run: dict[str, Any] | None = None
            x_shift = x_slope * (y - header_y)
            aligned_line = _shift_line_x(line, x_shift)
            aligned_column_starts = [float(start) - x_shift for start in column_starts]
            aligned_observed_lanes = [
                float(lane) - x_shift
                for lane in header.get("observed_data_lanes", [])
            ]
            for original_run, aligned_run in zip(line["runs"], aligned_line["runs"]):
                if y <= anchor["y"] + 2.5:
                    boundary_pieces = _split_run_at_column_boundaries(
                        aligned_run,
                        column_boundaries,
                        collapse_overlapping_duplicates=is_ocr_page,
                        split_crossing_boxes=is_ocr_page,
                        split_crossing_runs_by_word_start=not is_ocr_page,
                        column_starts=aligned_column_starts,
                    )
                    for piece, column in boundary_pieces:
                        if (
                            piece.get("overlapping_duplicate_words_removed")
                            or piece.get("crossing_column_boundary")
                        ):
                            alignment_uncertain = True
                        if column is None:
                            lane_pieces = _split_run_on_observed_lanes(
                                piece, aligned_observed_lanes
                            )
                            for lane_piece in lane_pieces:
                                lane_column = _assign_run_to_geometry_column(
                                    lane_piece, aligned_column_starts, column_boundaries
                                )
                                if (
                                    is_ocr_page
                                    and len(aligned_observed_lanes) == len(headers)
                                    and 0 < lane_column < len(headers)
                                    and float(lane_piece["x0"]) < aligned_observed_lanes[lane_column] - 22.0
                                ):
                                    alignment_uncertain = True
                                if lane_column < len(headers):
                                    cells[lane_column].append(_clean_cell(lane_piece["text"]))
                            continue
                        if column >= len(headers):
                            continue
                        if (
                            is_ocr_page
                            and len(aligned_observed_lanes) == len(headers)
                            and 0 < column < len(headers)
                            and float(piece["x0"]) < aligned_observed_lanes[column] - 22.0
                        ):
                            alignment_uncertain = True
                        cells[column].append(_clean_cell(piece["text"]))
                    row_spans.append(original_run)
                    continue

                text = _clean_cell(aligned_run["text"])
                has_alpha = bool(re.search(r"[A-Za-zÀ-ž]", text))
                column = _assign_continuation_column(
                    aligned_run, header["runs"], aligned_column_starts,
                    previous_continuation_column, previous_continuation_run,
                )
                if not has_alpha:
                    # Numeric-only wrapped text can still belong to a wide text
                    # lane. Keep it only when its geometry starts at that lane;
                    # isolated numeric continuations elsewhere remain excluded.
                    if (
                        not is_ocr_page
                        or column is None
                        or not _is_wide_observed_text_lane(column, aligned_observed_lanes)
                        or abs(float(aligned_run["x0"]) - aligned_column_starts[column]) > 22.0
                    ):
                        previous_continuation_column = None
                        previous_continuation_run = None
                        continue
                if column is None or column >= len(headers):
                    previous_continuation_column = None
                    previous_continuation_run = None
                    continue
                if (
                    is_ocr_page
                    and len(aligned_observed_lanes) == len(headers)
                    and 0 < column < len(headers)
                    and float(aligned_run["x0"]) < aligned_observed_lanes[column] - 22.0
                ):
                    alignment_uncertain = True
                cells[column].append(text)
                row_spans.append(original_run)
                previous_continuation_column = column
                previous_continuation_run = original_run

        clean_cells = [re.sub(r"\s+", " ", " ".join(cell)).strip() for cell in cells]
        # A candidate row must have content in the identifier cell and at least
        # one other column. Empty columns remain present to preserve structure.
        if not clean_cells[0] or sum(bool(value) for value in clean_cells[1:]) < 1:
            continue
        bbox = [
            round(min(float(run["x0"]) for run in row_spans), 2),
            round(min(float(run["y0"]) for run in row_spans), 2),
            round(max(float(run["x1"]) for run in row_spans), 2),
            round(max(float(run["y1"]) for run in row_spans), 2),
        ] if row_spans else [round(float(header["x0"]), 2), round(row_start, 2), round(float(header["x1"]), 2), round(row_end, 2)]
        row_records.append((clean_cells, bbox))
        geometry_row_review_reasons.append(
            ["row_alignment_uncertain"] if alignment_uncertain else []
        )
        item_bottom = max(item_bottom, bbox[3])

    if not row_records:
        return None, None

    table_bbox = [
        round(float(header["x0"]), 2),
        round(min(float(run["y0"]) for run in header["runs"]), 2),
        round(float(header["x1"]), 2),
        round(max(record[1][3] for record in row_records), 2),
    ]
    payload = {
        "table_index": 1,
        "table_role": "line_items",
        "layout_type": "item_table",
        "is_source_table": True,
        "headers": headers,
        "header_rows": [headers],
        "header_cells": [
            {"row": 0, "column": index, "text": str(label), "rowspan": 1, "colspan": 1, "source_bbox": None}
            for index, label in enumerate(headers)
        ],
        "header_detected": True,
        "header_source": "detected_from_page_geometry",
        "column_count": len(headers),
        "row_count": len(row_records),
        "rows": [record[0] for record in row_records],
        "rows_need_review": [bool(reasons) for reasons in geometry_row_review_reasons],
        "row_review_reasons": geometry_row_review_reasons,
        "context_rows": context_rows,
        "source_bbox": table_bbox,
        "header_source_bbox": [round(float(header["x0"]), 2), round(min(float(run["y0"]) for run in header["runs"]), 2), round(float(header["x1"]), 2), round(max(float(run["y1"]) for run in header["runs"]), 2)],
        "header_source_bboxes": [
            [round(float(header["x0"]), 2), round(min(float(run["y0"]) for run in header["runs"]), 2), round(float(header["x1"]), 2), round(max(float(run["y1"]) for run in header["runs"]), 2)],
            *header.get("stacked_header_bboxes", []),
        ],
        "row_source_bboxes": [record[1] for record in row_records],
        "detection_strategy": "geometry_header_anchored",
        "summary_items": [],
        "metadata_items": [],
        "detection_confidence": round(min(0.97, 0.62 + len(row_records) * 0.04 + float(header["coverage"]) * 0.12), 2),
    }
    return payload, float(table_bbox[3])


def _bbox_intersects(left: Any, right: Any) -> bool:
    """Return whether two source rectangles materially overlap."""
    if not left or not right or len(left) != 4 or len(right) != 4:
        return False
    x_overlap = min(float(left[2]), float(right[2])) - max(float(left[0]), float(right[0]))
    y_overlap = min(float(left[3]), float(right[3])) - max(float(left[1]), float(right[1]))
    if x_overlap <= 0 or y_overlap <= 0:
        return False
    left_width = max(1.0, float(left[2]) - float(left[0]))
    return x_overlap / left_width >= 0.18


def _is_numbered_subfield(value: str) -> bool:
    """Return True for a numbered label with words, not a bare line number."""
    return bool(re.match(r"^\s*\d+\s*[.)]\s*[A-Za-zÀ-ž]", _clean_cell(value)))


def _is_colon_run(value: str) -> bool:
    return _clean_cell(value).strip() in {":", "："}


def _metadata_pair_is_plausible(label: str, value: str) -> bool:
    """Qualify source-derived label/value pairs using shape, not field names."""
    label = _clean_cell(label).strip(" ,;: \t–—-")
    value = _clean_cell(value).strip(" :\t–—-")
    if not label or not value or len(label) > 64 or len(value) > 180:
        return False
    if len(label.split()) > 8 or not re.search(r"[A-Za-zÀ-ž]", label):
        return False
    # Numbered subfields such as "1) Maker" / "2) Origin" are legitimate labels.
    # Bare numbers and list markers remain invalid labels.
    if re.fullmatch(r"[\d.,/-]+", label) or label.endswith(("?", "!")):
        return False
    if re.match(r"^\s*\d+\s*[.)]\s*$", label):
        return False
    # Long sentence fragments ending with a period commonly occur when PDF
    # paragraphs happen to contain a large visual gap. Short abbreviations such
    # as "Our ref." remain eligible.
    if label.endswith(".") and len(label) > 20:
        return False
    if len(label) > 42 and len(label.split()) > 5:
        return False
    if value in {":", "-", "—", "_", "__"} or value.rstrip().endswith(":") or not re.search(r"[A-Za-z0-9]", value):
        return False
    if label.strip("_()[] ") == "" or not re.search(r"[A-Za-zÀ-ž]", label):
        return False
    return True


def _metadata_pairs_from_runs(runs: list[dict[str, Any]]) -> list[tuple[str, str, list[float]]]:
    """Extract multiple generic label/value pairs from a visual text baseline.

    The previous implementation discarded a baseline whenever it had more than
    two visual runs. Real PDFs often place two fields on one line, or include an
    unlabeled trailing note after a valid label/value pair. This routine first
    honors explicit colons, then walks remaining runs as adjacent label/value
    pairs when their geometry supports the relationship. No field-name whitelist
    is used.
    """
    pairs: list[tuple[str, str, list[float]]] = []
    consumed: set[int] = set()

    def add_pair(label: str, value: str, indexes: set[int]) -> bool:
        label = _clean_cell(label).strip(" ,;: \t–—-")
        value = _clean_cell(value).strip(" :\t–—-")
        if not _metadata_pair_is_plausible(label, value):
            return False
        selected = [index for index in indexes if 0 <= index < len(runs)]
        if not selected:
            return False
        bbox = [
            min(float(runs[i]["x0"]) for i in selected),
            min(float(runs[i]["y0"]) for i in selected),
            max(float(runs[i]["x1"]) for i in selected),
            max(float(runs[i]["y1"]) for i in selected),
        ]
        pairs.append((label, value, bbox))
        consumed.update(selected)
        return True

    def next_run_is_a_label(next_index: int) -> bool:
        """Detect a label token whose following run is its colon delimiter."""
        if next_index < 0 or next_index >= len(runs):
            return False
        next_text = _clean_cell(runs[next_index].get("text", ""))
        if ":" in next_text or next_text.endswith((":", "：")):
            return True
        return next_index + 1 < len(runs) and _is_colon_run(runs[next_index + 1].get("text", ""))

    # Pass 1: explicit colons. An attached label colon ("Customer reference:")
    # owns the immediately following value even if another field appears later
    # on that same baseline ("Delivery: Houston").
    for index, run in enumerate(runs):
        if index in consumed:
            continue
        text = _clean_cell(run.get("text", ""))
        if ":" not in text and "：" not in text:
            continue
        delimiter = ":" if ":" in text else "："
        if _is_colon_run(text):
            previous = index - 1
            following = index + 1
            if previous < 0 or following >= len(runs):
                continue
            if previous in consumed or following in consumed:
                continue
            # In the pattern "Fax : Required Date : 2/20/2026", the token after
            # this colon is another label, not its value. Do not create Fax -> Required Date.
            if next_run_is_a_label(following):
                continue
            gap = float(runs[following]["x0"]) - float(runs[previous]["x1"])
            if gap < -2:
                continue
            add_pair(runs[previous].get("text", ""), runs[following].get("text", ""), {previous, index, following})
            continue

        before, after = text.split(delimiter, 1)
        label, value = before.strip(), after.strip()
        indexes = {index}
        if label and not value:
            following = index + 1
            if following < len(runs) and following not in consumed:
                candidate_value = _clean_cell(runs[following].get("text", ""))
                # Do not absorb a following label when the source represents its
                # colon as a separate run. Otherwise the immediate run is the
                # value, irrespective of any later field on this line.
                if candidate_value and not _is_colon_run(candidate_value) and not next_run_is_a_label(following):
                    value = candidate_value
                    indexes.add(following)
        elif not label and value and index > 0 and index - 1 not in consumed:
            label = _clean_cell(runs[index - 1].get("text", ""))
            indexes.add(index - 1)
        # A URL can be split across the colon after its protocol ("https: //...").
        # This is a link, not a document field; detect the generic URI scheme form.
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]{0,20}", label) and value.lstrip().startswith("//"):
            continue
        if value and add_pair(label, value, indexes):
            continue

    # Pass 2: no selectable colon. Process all remaining runs from left to right
    # in pairs, instead of requiring exactly two runs on the full baseline.
    remaining = [index for index in range(len(runs)) if index not in consumed]
    cursor = 0
    while cursor + 1 < len(remaining):
        label_index = remaining[cursor]
        value_index = remaining[cursor + 1]
        label_run = runs[label_index]
        value_run = runs[value_index]
        label = _clean_cell(label_run.get("text", ""))
        value = _clean_cell(value_run.get("text", ""))

        if ":" in label or "：" in label or ":" in value or "：" in value or _is_colon_run(value):
            cursor += 1
            continue
        # A numbered token with alphabetic text is itself a subfield label. For
        # example, in "Remarks | 1) Maker | Fukui Seisakusho", do not make the
        # incorrect pair Remarks -> 1) Maker; allow 1) Maker -> Fukui Seisakusho.
        if _is_numbered_subfield(value):
            cursor += 1
            continue

        gap = float(value_run["x0"]) - float(label_run["x1"])
        heights = [
            max(1.0, float(word.get("y1", 0)) - float(word.get("y0", 0)))
            for word in (label_run.get("words", []) or value_run.get("words", []) or [label_run, value_run])
        ]
        median_height = float(median(heights)) if heights else 10.0
        if gap < max(12.0, median_height * 1.3):
            cursor += 1
            continue

        if add_pair(label, value, {label_index, value_index}):
            cursor += 2
        else:
            cursor += 1

    return pairs

def _extract_document_metadata(
    page: Any,
    tables: list[dict[str, Any]],
    summary_candidates: list[dict[str, Any]],
    visual_lines: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Extract general label/value metadata outside table-header/item regions."""
    lines = visual_lines if visual_lines is not None else _word_runs_on_visual_baselines(page)
    excluded_bboxes: list[list[float]] = []
    for table in tables:
        # A weak OCR reconstruction can omit or misplace individual row boxes.
        # Exclude the enclosing source-table region as a final safeguard so
        # item/header text inside that region cannot leak into document metadata.
        source_bbox = table.get("source_bbox")
        if source_bbox and len(source_bbox) == 4:
            excluded_bboxes.append([float(value) for value in source_bbox])
        # Prefer individually located header rows/fragments when supplied. This
        # excludes actual header text (including stacked units) without swallowing
        # unrelated metadata just above or below the item table.
        header_boxes = table.get("header_source_bboxes") or []
        if not header_boxes and table.get("header_source_bbox"):
            header_boxes = [table.get("header_source_bbox")]
        for bbox in header_boxes:
            if bbox and len(bbox) == 4:
                excluded_bboxes.append([
                    float(bbox[0]) - 1.5, max(0.0, float(bbox[1]) - 1.5),
                    float(bbox[2]) + 1.5, float(bbox[3]) + 1.5,
                ])
        for bbox in table.get("row_source_bboxes", []) or []:
            if bbox:
                excluded_bboxes.append(list(bbox))
    for item in summary_candidates:
        bbox = item.get("source_bbox")
        if bbox:
            excluded_bboxes.append(list(bbox))

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    page_number = int(getattr(page, "number", 0)) + 1
    for line in lines:
        runs = line.get("runs", [])
        if not runs:
            continue
        # Some PDFs expose "Label: value" as one positioned span. Do not skip
        # those rows merely because text extraction did not split the span.
        line_bbox = [
            min(float(run["x0"]) for run in runs),
            min(float(run["y0"]) for run in runs),
            max(float(run["x1"]) for run in runs),
            max(float(run["y1"]) for run in runs),
        ]
        # Printed-page timestamps and running footer text often look like a
        # label/value pair when a PDF was assembled from email or attachments.
        # Exclude only the very bottom geometric band from *metadata* inference;
        # native table extraction and source content remain untouched.
        page_height = float(getattr(getattr(page, "rect", None), "height", 0.0) or 0.0)
        footer_band = max(14.0, page_height * 0.022)
        if page_height > 0 and line_bbox[3] >= page_height - footer_band:
            continue
        if any(_bbox_intersects(line_bbox, excluded) for excluded in excluded_bboxes):
            continue
        for label, value, bbox in _metadata_pairs_from_runs(runs):
            if any(_bbox_intersects(bbox, excluded) for excluded in excluded_bboxes):
                continue
            cleaned_label = _clean_cell(label).strip(" :\t–—-")
            cleaned_value = _clean_cell(value)
            footer_uri = f"{cleaned_label}:{cleaned_value}"
            if (
                page_height > 0
                and line_bbox[1] >= page_height * 0.82
                and re.match(r"^(?:about|https?|ftp|file):", footer_uri, re.IGNORECASE)
            ):
                # Browser/print pipelines can stamp a page URL such as
                # about:blank into the lower margin. It is a page artifact,
                # not document metadata.
                continue
            key = (cleaned_label.casefold(), cleaned_value.casefold(), round(float(line["y"])))
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "label": cleaned_label,
                "value": cleaned_value,
                "source_page": page_number,
                "source_bbox": [round(float(value), 2) for value in bbox],
            })
    return results


def _build_document_metadata_table(
    metadata_items: list[dict[str, Any]],
    table_index: int | None = None,
) -> dict[str, Any] | None:
    """Build one source-linked, two-column table from page label/value pairs.

    This is a logical display table, not a native grid found in the PDF. It is
    intentionally created once per page so metadata pairs scattered across a
    header/form area do not become many small visual tables. Labels, values and
    positions are retained verbatim from the source-derived metadata records.
    """
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    boxes: list[list[float]] = []
    for item in metadata_items:
        label = _clean_cell(item.get("label", ""))
        value = _clean_cell(item.get("value", ""))
        if not label or not value:
            continue
        bbox = item.get("source_bbox")
        y_key = round((float(bbox[1]) + float(bbox[3])) / 2) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else 0
        key = (label.casefold(), value.casefold(), y_key)
        if key in seen:
            continue
        seen.add(key)
        copied = dict(item)
        copied["label"] = label
        copied["value"] = value
        cleaned.append(copied)
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                coordinates = [float(value) for value in bbox]
            except (TypeError, ValueError):
                continue
            if all(value == value and abs(value) != float("inf") for value in coordinates):
                boxes.append(coordinates)

    if not cleaned:
        return None

    # Sort using source reading order, without grouping by arbitrary whitespace
    # thresholds. A page can have left and right metadata columns; their original
    # label/value text and bounding boxes stay attached to each row.
    cleaned.sort(key=lambda item: (
        (float(item["source_bbox"][1]) + float(item["source_bbox"][3])) / 2
        if isinstance(item.get("source_bbox"), (list, tuple)) and len(item["source_bbox"]) == 4
        else float("inf"),
        float(item["source_bbox"][0]) if isinstance(item.get("source_bbox"), (list, tuple)) and len(item["source_bbox"]) == 4 else float("inf"),
    ))

    source_bbox = None
    if boxes:
        source_bbox = [
            round(min(box[0] for box in boxes), 2),
            round(min(box[1] for box in boxes), 2),
            round(max(box[2] for box in boxes), 2),
            round(max(box[3] for box in boxes), 2),
        ]

    return {
        "table_index": table_index,
        "table_role": "document_metadata",
        "layout_type": "document_metadata",
        "is_source_table": False,
        "headers": [],
        "header_rows": [],
        "header_cells": [],
        "header_detected": False,
        "header_source": "not_applicable_label_value_layout",
        "column_count": 2,
        "row_count": len(cleaned),
        "rows": [[item["label"], item["value"]] for item in cleaned],
        "metadata_items": cleaned,
        "summary_items": [],
        "source_bbox": source_bbox,
        "row_source_bboxes": [item.get("source_bbox") for item in cleaned],
        "detection_strategy": "page_level_label_value_pairs",
        "detection_confidence": None,
    }


def _public_summary_items(candidates: list[dict[str, Any]], data_bottom: float | None) -> list[dict[str, Any]]:
    if data_bottom is None:
        return []
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for item in candidates:
        if item["_y"] < data_bottom - 3.5:
            continue
        key = (item["label"].casefold(), item["value"], round(item["_y"]))
        if key in seen:
            continue
        seen.add(key)
        results.append({key: value for key, value in item.items() if not key.startswith("_")})
    return results


def _detach_ocr_summary_candidates_from_item_rows(
    page: Any,
    tables: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    data_bottom: float | None,
) -> float | None:
    """Separate a positioned summary accidentally absorbed into the last OCR row.

    OCR line grouping can extend the last item's description box into a separate
    label/amount line below the item sequence. Detach only when the summary is
    geometrically inside that row, below its independently recognized item
    identifier, and its exact label/value text is present in a cell. No summary
    text or amount is inferred or rewritten.
    """
    if not getattr(page, "ocr_word_count", 0):
        return data_bottom
    page_items = [item for item in getattr(page, "items", []) if isinstance(item, dict)]

    def normalized_identifier(value: Any) -> str:
        return _clean_cell(value).strip().rstrip(".)").casefold()

    def remove_summary_phrase(text: str, label: str, value: str) -> str | None:
        label_words = r"\s+".join(re.escape(part) for part in label.split())
        value_pattern = re.escape(value.strip())
        pattern = re.compile(
            rf"(?<![\w]){label_words}\s*[:：]?\s*{value_pattern}(?![\w])",
            re.IGNORECASE,
        )
        cleaned, replacements = pattern.subn(" ", text, count=1)
        if not replacements:
            return None
        return _clean_cell(cleaned).strip(" :：")

    for candidate in candidates:
        summary_bbox = candidate.get("source_bbox")
        if not isinstance(summary_bbox, (list, tuple)) or len(summary_bbox) != 4:
            continue
        try:
            summary_box = [float(value) for value in summary_bbox]
            summary_y = float(candidate["_y"])
        except (KeyError, TypeError, ValueError):
            continue
        if summary_box[2] <= summary_box[0] or summary_box[3] <= summary_box[1]:
            continue

        for table in tables:
            if table.get("table_role", "line_items") == "document_header":
                continue
            rows = table.get("rows")
            row_boxes = table.get("row_source_bboxes") or []
            if not isinstance(rows, list) or not rows:
                continue
            overlapping_rows = [
                row_index for row_index, row_box in enumerate(row_boxes[:len(rows)])
                if isinstance(row_box, (list, tuple)) and len(row_box) == 4
                and _bbox_intersects(summary_box, [float(value) for value in row_box])
            ]
            if not overlapping_rows:
                continue

            # Summary text is only detached from the final source row, and only
            # when the OCR identifier on that row proves the summary is on a
            # later baseline. This leaves same-line item descriptions intact.
            row_index = max(overlapping_rows)
            row = rows[row_index]
            if not isinstance(row, (list, tuple)) or not row or not _is_item_identifier(_clean_cell(row[0])):
                continue
            row_box = row_boxes[row_index]
            identifier = normalized_identifier(row[0])
            identifier_cell_boxes = table.get("cell_source_bboxes") or []
            identifier_box = (
                identifier_cell_boxes[row_index][0]
                if row_index < len(identifier_cell_boxes)
                and identifier_cell_boxes[row_index]
                else None
            )
            id_matches: list[dict[str, Any]] = []
            for item in page_items:
                if normalized_identifier(item.get("text", "")) != identifier:
                    continue
                try:
                    item_box = [float(item[key]) for key in ("x0", "y0", "x1", "y1")]
                except (KeyError, TypeError, ValueError):
                    continue
                if not _bbox_intersects(item_box, [float(value) for value in row_box]):
                    continue
                if (
                    isinstance(identifier_box, (list, tuple))
                    and len(identifier_box) == 4
                    and not _bbox_intersects(item_box, [float(value) for value in identifier_box])
                ):
                    continue
                id_matches.append({"bbox": item_box, "text": item.get("text", "")})
            if not id_matches:
                continue
            id_word = min(id_matches, key=lambda item: float(item["bbox"][0]))
            id_bbox = id_word["bbox"]
            id_height = max(1.0, id_bbox[3] - id_bbox[1])
            if summary_y <= id_bbox[3] + max(3.0, id_height * 0.55):
                continue

            label, value = str(candidate.get("label", "")).strip(), str(candidate.get("value", "")).strip()
            if not label or not value:
                continue
            values = list(row)
            cell_boxes = table.get("cell_source_bboxes") or []
            changed_columns: list[int] = []
            for column_index, cell in enumerate(values):
                cell_box = (
                    cell_boxes[row_index][column_index]
                    if row_index < len(cell_boxes) and column_index < len(cell_boxes[row_index])
                    else row_box
                )
                if not isinstance(cell_box, (list, tuple)) or len(cell_box) != 4:
                    continue
                if not _bbox_intersects(summary_box, [float(value) for value in cell_box]):
                    continue
                cleaned = remove_summary_phrase(_clean_cell(cell), label, value)
                if cleaned is None:
                    continue
                values[column_index] = cleaned
                changed_columns.append(column_index)
            if not changed_columns:
                continue

            if isinstance(row, tuple):
                rows[row_index] = tuple(values)
            else:
                rows[row_index] = values
            new_bottom = max(float(row_box[1]), summary_box[1] - 1.0)
            row_boxes[row_index] = [*map(float, row_box[:3]), new_bottom]
            table["row_source_bboxes"] = row_boxes
            for box_collection_name in ("cell_source_bboxes", "row_band_bboxes"):
                box_collection = table.get(box_collection_name) or []
                if row_index >= len(box_collection):
                    continue
                row_cells = box_collection[row_index]
                if not isinstance(row_cells, list):
                    continue
                for column_index in changed_columns:
                    if column_index >= len(row_cells):
                        continue
                    cell_box = row_cells[column_index]
                    if isinstance(cell_box, (list, tuple)) and len(cell_box) == 4:
                        row_cells[column_index] = [
                            float(cell_box[0]), float(cell_box[1]), float(cell_box[2]),
                            min(float(cell_box[3]), new_bottom),
                        ]
                box_collection[row_index] = row_cells
                table[box_collection_name] = box_collection
            source_box = table.get("source_bbox")
            if isinstance(source_box, (list, tuple)) and len(source_box) == 4:
                table["source_bbox"] = [
                    float(source_box[0]), float(source_box[1]), float(source_box[2]),
                    min(float(source_box[3]), new_bottom),
                ]
            data_bottom = new_bottom if data_bottom is None else min(data_bottom, new_bottom)
            break
    return data_bottom


def _ocr_confidence_for_bbox(page: Any, bbox: Any) -> float | None:
    """Average confidences of OCR word boxes intersecting a source region."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None

    confidences: list[float] = []
    for item in getattr(page, "items", []):
        confidence = item.get("confidence") if isinstance(item, dict) else None
        if confidence is None:
            continue
        try:
            confidence = float(confidence)
            ix0, iy0 = float(item["x0"]), float(item["y0"])
            ix1, iy1 = float(item["x1"]), float(item["y1"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(x1, ix1) > max(x0, ix0) and min(y1, iy1) > max(y0, iy0):
            confidences.append(max(0.0, min(1.0, confidence)))
    return round(sum(confidences) / len(confidences), 4) if confidences else None


def _ocr_cell_evidence(page: Any, bbox: Any, final_value: Any) -> dict[str, Any]:
    """Keep the OCR words and confidence associated with one extracted cell.

    This is provenance only: the OCR text is never substituted into the final
    table value here.  In particular, punctuation-only OCR tokens are retained
    and an empty OCR region is reported as ``no_text_detected`` rather than
    being presented as proof that the source cell is physically blank.
    """
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        x0 = y0 = x1 = y1 = 0.0
    valid_bbox = x1 > x0 and y1 > y0
    raw_words: list[dict[str, Any]] = []
    if valid_bbox:
        for item in getattr(page, "items", []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("raw_text", item.get("text", "")))
            if not text.strip():
                continue
            try:
                ix0, iy0 = float(item["x0"]), float(item["y0"])
                ix1, iy1 = float(item["x1"]), float(item["y1"])
            except (KeyError, TypeError, ValueError):
                continue
            if min(x1, ix1) <= max(x0, ix0) or min(y1, iy1) <= max(y0, iy0):
                continue
            raw_words.append({
                "text": text,
                "confidence": item.get("confidence"),
                "source_bbox": [round(ix0, 2), round(iy0, 2), round(ix1, 2), round(iy1, 2)],
                "_order": ((iy0 + iy1) / 2.0, ix0),
            })
    raw_words.sort(key=lambda word: word["_order"])
    for word in raw_words:
        word.pop("_order", None)

    raw_text = " ".join(word["text"] for word in raw_words)
    confidences: list[float] = []
    for word in raw_words:
        try:
            confidence = float(word["confidence"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(confidence):
            confidences.append(max(0.0, min(1.0, confidence)))
    mean_confidence = (
        round(sum(confidences) / len(confidences), 4) if confidences else None
    )
    final_text = _clean_cell(final_value)
    raw_present = bool(raw_text.strip())
    final_present = bool(final_text)
    if not valid_bbox:
        status = "bbox_unavailable"
    elif not raw_present and not final_present:
        status = "no_text_detected"
    elif not raw_present:
        status = "no_direct_ocr_word"
    elif not final_present:
        status = "ocr_text_not_assigned"
    elif _clean_cell(raw_text) != final_text:
        status = "final_value_differs_from_raw_ocr"
    else:
        status = "recognized"
    return {
        "raw_text": raw_text,
        "words": raw_words,
        "confidence": mean_confidence,
        "status": status,
        "source_bbox": [round(value, 2) for value in (x0, y0, x1, y1)] if valid_bbox else None,
        "final_value": final_text,
        # OCR absence cannot prove a physically empty source cell. Keep that
        # distinction explicit so callers do not mistake missing recognition
        # for a verified blank.
        "blankness": "unknown" if not final_present else None,
    }


def _attach_ocr_cell_evidence(page: Any, table: dict[str, Any]) -> None:
    """Attach raw OCR provenance after all table value recovery/validation."""
    rows = table.get("rows")
    if not isinstance(rows, list):
        return
    count = max(
        int(table.get("column_count") or 0),
        max((len(row) for row in rows if isinstance(row, (list, tuple))), default=0),
    )
    boxes = table.get("cell_source_bboxes") or []
    evidence: list[list[dict[str, Any]]] = []
    for row_index, row in enumerate(rows):
        values = list(row) if isinstance(row, (list, tuple)) else []
        cell_boxes = boxes[row_index] if row_index < len(boxes) else []
        evidence.append([
            _ocr_cell_evidence(
                page,
                cell_boxes[column_index] if column_index < len(cell_boxes) else None,
                values[column_index] if column_index < len(values) else "",
            )
            for column_index in range(count)
        ])
    table["cell_ocr_data"] = evidence


def _has_ocr_text_in_bbox(page: Any, bbox: Any) -> bool:
    """Whether OCR actually returned a non-empty word inside a source cell."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return False
    if x1 <= x0 or y1 <= y0:
        return False
    for item in getattr(page, "items", []):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        try:
            ix0, iy0 = float(item["x0"]), float(item["y0"])
            ix1, iy1 = float(item["x1"]), float(item["y1"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(x1, ix1) > max(x0, ix0) and min(y1, iy1) > max(y0, iy0):
            return True
    return False


def _annotate_ocr_table_review(page: Any, table: dict[str, Any]) -> None:
    """Attach source-linked review flags without inventing rows or values."""
    rows = table.get("rows")
    if not isinstance(rows, list):
        return
    column_count = max(
        int(table.get("column_count") or 0),
        max((len(row) for row in rows if isinstance(row, (list, tuple))), default=0),
    )
    row_flags = list(table.get("rows_need_review") or [])
    row_reasons = list(table.get("row_review_reasons") or [])
    while len(row_flags) < len(rows):
        row_flags.append(False)
    while len(row_reasons) < len(rows):
        row_reasons.append([])
    cell_flags: list[list[bool]] = []
    row_confidences: list[float | None] = []
    cell_confidences: list[list[float | None]] = []
    issues: list[dict[str, Any]] = []
    row_bboxes = table.get("row_source_bboxes") or []
    cell_bboxes = table.get("cell_source_bboxes") or []

    numeric_lane_columns: set[int] = set()
    for column_index in range(column_count):
        column_values = [
            _clean_cell(row[column_index])
            for row in rows
            if isinstance(row, (list, tuple))
            and column_index < len(row)
            and _clean_cell(row[column_index])
        ]
        numeric_count = sum(_is_numeric_token(value) for value in column_values)
        if (
            len(column_values) >= 5
            and numeric_count >= 4
            and numeric_count / len(column_values) >= 0.70
        ):
            numeric_lane_columns.add(column_index)

    if not table.get("header_detected"):
        for row_index in range(len(rows)):
            row_flags[row_index] = True
            if "header_not_detected" not in row_reasons[row_index]:
                row_reasons[row_index].append("header_not_detected")
        issues.append({
            "code": "header_not_detected",
            "message": (
                "Header kolom tidak berhasil dikenali. Susunan kolom dibentuk dari posisi isi OCR dan "
                "semua baris ditandai untuk diperiksa; cocokkan label serta posisi nilainya dengan PDF sumber."
            ),
            "source_bbox": table.get("source_bbox"),
        })

    for row_index, reasons in enumerate(row_reasons):
        if "row_alignment_uncertain" in reasons:
            issues.append({
                "code": "row_alignment_uncertain",
                "message": "Baris kandidat dipertahankan meski alignment-nya menyimpang; periksa posisi sel pada sumber.",
                "row_index": row_index,
                "source_bbox": row_bboxes[row_index] if row_index < len(row_bboxes) else None,
            })

    for row_index, row in enumerate(rows):
        values = list(row) if isinstance(row, (list, tuple)) else []
        flags = [False] * column_count
        confidences: list[float | None] = []
        for column_index in range(column_count):
            value = _clean_cell(values[column_index]) if column_index < len(values) else ""
            bbox = (
                cell_bboxes[row_index][column_index]
                if row_index < len(cell_bboxes)
                and column_index < len(cell_bboxes[row_index])
                else None
            )
            confidence = _ocr_confidence_for_bbox(page, bbox)
            confidences.append(confidence)
            if not value and _has_ocr_text_in_bbox(page, bbox):
                flags[column_index] = True
                row_flags[row_index] = True
                if "ocr_text_not_assigned_to_cell" not in row_reasons[row_index]:
                    row_reasons[row_index].append("ocr_text_not_assigned_to_cell")
                issues.append({
                    "code": "ocr_text_not_assigned_to_cell",
                    "message": (
                        "Teks OCR terdeteksi di area sel ini, tetapi nilai hasil ekstraksi kosong; "
                        "periksa apakah nilai terlewat atau bergeser."
                    ),
                    "row_index": row_index,
                    "column_index": column_index,
                    "source_bbox": bbox,
                })
            elif not value and column_index in numeric_lane_columns:
                # A blank in a lane that is numeric in the overwhelming
                # majority of neighboring rows is a possible missed OCR cell.
                # Keep it blank unless an independent repeated relationship
                # can recover it; always expose the uncertainty for review.
                flags[column_index] = True
                row_flags[row_index] = True
                if "possible_missing_numeric_cell" not in row_reasons[row_index]:
                    row_reasons[row_index].append("possible_missing_numeric_cell")
                issues.append({
                    "code": "possible_missing_numeric_cell",
                    "message": "Sel kosong berada pada kolom yang umumnya berisi angka; cocokkan dengan PDF sumber.",
                    "row_index": row_index,
                    "column_index": column_index,
                    "source_bbox": bbox or (row_bboxes[row_index] if row_index < len(row_bboxes) else None),
                })
            elif value and column_index in numeric_lane_columns:
                numeric_tokens = _NUMERIC_TOKEN_RE.findall(value)
                has_letters = bool(re.search(r"[A-Za-zÀ-ž]", value))
                if has_letters or len(numeric_tokens) > 1:
                    flags[column_index] = True
                    row_flags[row_index] = True
                    if "unexpected_text_in_numeric_lane" not in row_reasons[row_index]:
                        row_reasons[row_index].append("unexpected_text_in_numeric_lane")
                    issues.append({
                        "code": "unexpected_text_in_numeric_lane",
                        "message": (
                            "Sel berada pada kolom yang biasanya berisi satu angka, tetapi OCR menghasilkan "
                            "teks atau beberapa angka; periksa kemungkinan nilai bergeser atau tergabung."
                        ),
                        "row_index": row_index,
                        "column_index": column_index,
                        "value": value,
                        "source_bbox": bbox or (row_bboxes[row_index] if row_index < len(row_bboxes) else None),
                    })
            elif confidence is not None and confidence < 0.75:
                flags[column_index] = True
                row_flags[row_index] = True
                if "low_ocr_confidence" not in row_reasons[row_index]:
                    row_reasons[row_index].append("low_ocr_confidence")
                issues.append({
                    "code": "low_ocr_confidence",
                    "message": f"Confidence OCR sel ini {confidence:.0%}; cocokkan teks dengan PDF sumber.",
                    "row_index": row_index,
                    "column_index": column_index,
                    "confidence": confidence,
                    "source_bbox": bbox or (row_bboxes[row_index] if row_index < len(row_bboxes) else None),
                })
        cell_flags.append(flags)
        cell_confidences.append(confidences)

        row_bbox = row_bboxes[row_index] if row_index < len(row_bboxes) else None
        row_confidence = _ocr_confidence_for_bbox(page, row_bbox)
        row_confidences.append(row_confidence)
        if row_confidence is not None and row_confidence < 0.75:
            row_flags[row_index] = True
            if "low_ocr_confidence" not in row_reasons[row_index]:
                row_reasons[row_index].append("low_ocr_confidence")

        if len(values) != column_count:
            row_flags[row_index] = True
            if "column_count_mismatch" not in row_reasons[row_index]:
                row_reasons[row_index].append("column_count_mismatch")
            issues.append({
                "code": "column_count_mismatch",
                "message": (
                    f"Baris ini berisi {len(values)} sel, sedangkan struktur tabel memiliki "
                    f"{column_count} kolom; periksa kemungkinan sel bergeser atau terlewat."
                ),
                "row_index": row_index,
                "source_bbox": row_bbox,
            })

    identifiers: list[tuple[int, int]] = []
    for row_index, row in enumerate(rows):
        first_value = _clean_cell(row[0]) if isinstance(row, (list, tuple)) and row else ""
        if re.fullmatch(r"\d+", first_value):
            identifiers.append((row_index, int(first_value)))
    positions_by_id: dict[int, list[int]] = {}
    for row_index, identifier in identifiers:
        positions_by_id.setdefault(identifier, []).append(row_index)
    for identifier, positions in positions_by_id.items():
        if len(positions) < 2:
            continue
        for row_index in positions:
            row_flags[row_index] = True
            if "duplicate_identifier" not in row_reasons[row_index]:
                row_reasons[row_index].append("duplicate_identifier")
        issues.append({
            "code": "duplicate_identifier",
            "message": f"Nomor item {identifier} muncul lebih dari sekali.",
            "identifier": str(identifier),
            "row_indices": positions,
            "source_bbox": [row_bboxes[index] for index in positions if index < len(row_bboxes)],
        })

    numeric_ids = [identifier for _, identifier in identifiers]
    deltas = [right - left for left, right in zip(numeric_ids, numeric_ids[1:])]
    positive_deltas = [delta for delta in deltas if delta > 0]
    sequential_ratio = (
        sum(delta == 1 for delta in positive_deltas) / len(positive_deltas)
        if positive_deltas else 0.0
    )
    if len(set(numeric_ids)) >= 5 and sequential_ratio >= 0.75:
        for left, right in zip(identifiers, identifiers[1:]):
            left_row, left_id = left
            right_row, right_id = right
            if right_id <= left_id + 1:
                continue
            suspected = list(range(left_id + 1, right_id))
            for row_index in (left_row, right_row):
                row_flags[row_index] = True
                if "possible_sequence_gap" not in row_reasons[row_index]:
                    row_reasons[row_index].append("possible_sequence_gap")
            previous_bbox = row_bboxes[left_row] if left_row < len(row_bboxes) else None
            next_bbox = row_bboxes[right_row] if right_row < len(row_bboxes) else None
            gap_bbox = None
            if previous_bbox and next_bbox:
                gap_bbox = [
                    min(float(previous_bbox[0]), float(next_bbox[0])),
                    min(float(previous_bbox[3]), float(next_bbox[1])),
                    max(float(previous_bbox[2]), float(next_bbox[2])),
                    max(float(previous_bbox[3]), float(next_bbox[1])),
                ]
            issues.append({
                "code": "possible_sequence_gap",
                "message": (
                    f"Nomor item {', '.join(map(str, suspected))} tidak ada di hasil. "
                    "Ini petunjuk untuk diperiksa, bukan bukti bahwa baris tersebut ada di sumber."
                ),
                "identifiers": [str(value) for value in suspected],
                "row_indices": [left_row, right_row],
                "source_bbox": gap_bbox,
            })

    table["rows_need_review"] = row_flags
    table["row_review_reasons"] = row_reasons
    table["cell_review_flags"] = cell_flags
    table["row_confidence"] = row_confidences
    table["cell_confidence"] = cell_confidences
    table["review_issues"] = issues
    table["review_required"] = bool(issues or any(row_flags))
    table["validation_status"] = "needs_review" if table["review_required"] else "no_detected_anomalies"


def _recover_missing_ocr_text_line_from_geometry(page: Any, table: dict[str, Any]) -> None:
    """Recover a missed first text line when the rest of the row proves its lane.

    A small crop is attempted only when a text-dense cell already has OCR text
    on a lower baseline, the same row has aligned OCR anchors on both sides of
    that text lane, and the anchor baseline is otherwise empty. Column lanes
    come from neighboring observed cell words and headers; no document schema
    or fixed column position is assumed.
    """
    rows = table.get("rows")
    items = getattr(page, "items", None)
    row_boxes = table.get("row_source_bboxes") or []
    if not isinstance(rows, list) or not isinstance(items, list) or len(rows) < 3:
        return
    column_count = max(
        int(table.get("column_count") or 0),
        max((len(row) for row in rows if isinstance(row, (list, tuple))), default=0),
    )
    if column_count < 3:
        return

    def valid_box(box: Any) -> bool:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            return False
        try:
            x0, y0, x1, y1 = (float(value) for value in box)
        except (TypeError, ValueError):
            return False
        return x1 > x0 and y1 > y0

    def box_of(item: Any) -> tuple[float, float, float, float] | None:
        try:
            box = tuple(float(item[key]) for key in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            return None
        return box if box[2] > box[0] and box[3] > box[1] else None

    def compact(value: Any) -> str:
        return "".join(re.findall(r"[a-z0-9]+", _clean_cell(value).casefold()))

    def token_set(value: Any) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", _clean_cell(value).casefold()))

    def cell_token_count(value: Any) -> int:
        return len(re.findall(r"\S+", _clean_cell(value)))

    items_by_row: list[list[tuple[tuple[float, ...], Any]]] = []
    for row_index in range(len(rows)):
        row_items: list[tuple[tuple[float, ...], Any]] = []
        row_box = row_boxes[row_index] if row_index < len(row_boxes) else None
        if valid_box(row_box):
            normalized_row_box = tuple(float(value) for value in row_box)
            for item in items:
                box = box_of(item)
                if box is not None and _bbox_overlap_ratio(box, normalized_row_box) >= 0.10:
                    row_items.append((box, item))
        items_by_row.append(row_items)

    # Long, repeated cell text is inferred from the extracted rows themselves.
    # If multiple columns are similarly text-dense, the geometry is ambiguous.
    density: list[tuple[float, float, int]] = []
    for column_index in range(column_count):
        counts = [
            cell_token_count(row[column_index])
            for row in rows
            if isinstance(row, (list, tuple)) and column_index < len(row)
            and _clean_cell(row[column_index])
        ]
        if counts:
            density.append((sum(counts) / len(counts), float(median(counts)), column_index))
    density.sort(reverse=True)
    if not density:
        return
    mean_density, median_density, text_column = density[0]
    runner_up = density[1][0] if len(density) > 1 else 0.0
    if median_density < 3 or mean_density < max(3.0, runner_up * 1.5):
        return
    if text_column == 0 or text_column == column_count - 1:
        return

    numeric_lane_columns: set[int] = set()
    for column_index in range(text_column + 1, column_count):
        values = [
            _clean_cell(row[column_index])
            for row in rows
            if isinstance(row, (list, tuple)) and column_index < len(row)
            and _clean_cell(row[column_index])
        ]
        numeric_count = sum(_is_numeric_token(value) for value in values)
        if len(values) >= 5 and numeric_count >= 4 and numeric_count / len(values) >= 0.70:
            numeric_lane_columns.add(column_index)

    header_centers: dict[int, float] = {}
    header_boxes = table.get("header_source_bboxes") or []
    if not header_boxes and valid_box(table.get("header_source_bbox")):
        header_boxes = [table["header_source_bbox"]]
    for column_index, header in enumerate(table.get("headers") or []):
        header_tokens = token_set(header)
        if not header_tokens:
            continue
        matched: list[float] = []
        for item in items:
            item_tokens = token_set(item.get("text", ""))
            box = box_of(item)
            if not box or not (header_tokens & item_tokens):
                continue
            if any(
                valid_box(header_box)
                and box[1] <= float(header_box[3]) + 2.0
                and box[3] >= float(header_box[1]) - 2.0
                for header_box in header_boxes
            ):
                matched.append((box[0] + box[2]) / 2.0)
        if matched:
            header_centers[column_index] = float(median(matched))

    observed_lanes: dict[int, dict[int, tuple[float, ...]]] = {}
    for column_index in (text_column - 1, text_column + 1):
        lane_matches: dict[int, tuple[float, ...]] = {}
        for other_index, row in enumerate(rows):
            if (
                other_index >= len(row_boxes)
                or not valid_box(row_boxes[other_index])
                or not isinstance(row, (list, tuple)) or column_index >= len(row)
            ):
                continue
            expected = compact(row[column_index])
            if not expected:
                continue
            row_box = tuple(float(value) for value in row_boxes[other_index])
            candidates: list[tuple[float, float, tuple[float, ...]]] = []
            for box, item in items_by_row[other_index]:
                if compact(item.get("text", "")) != expected:
                    continue
                center_x = (box[0] + box[2]) / 2.0
                header_center = header_centers.get(column_index)
                distance = abs(center_x - header_center) if header_center is not None else 0.0
                candidates.append((distance, box[0], box))
            if candidates:
                lane_matches[other_index] = min(candidates, key=lambda candidate: candidate[0])[2]
        observed_lanes[column_index] = lane_matches

    row_flags = table.setdefault("rows_need_review", [False] * len(rows))
    row_reasons = table.setdefault("row_review_reasons", [[] for _ in rows])
    cell_flags = table.setdefault(
        "cell_review_flags", [[False] * column_count for _ in rows]
    )
    issues = table.setdefault("review_issues", [])
    changed = False

    for row_index, row in enumerate(rows):
        if (
            not isinstance(row, list) or text_column >= len(row)
            or row_index >= len(row_boxes) or not valid_box(row_boxes[row_index])
        ):
            continue
        existing_text = _clean_cell(row[text_column])
        existing_tokens = token_set(existing_text)
        if len(existing_tokens) < 2:
            continue

        left_boxes = [
            box for other_index, box in observed_lanes.get(text_column - 1, {}).items()
            if other_index != row_index
        ]
        right_boxes = [
            box for other_index, box in observed_lanes.get(text_column + 1, {}).items()
            if other_index != row_index
        ]
        if len(left_boxes) < 2 or len(right_boxes) < 2:
            continue
        left_edge = float(median(box[2] for box in left_boxes))
        right_edge = float(median(box[0] for box in right_boxes))
        if right_edge - left_edge < 40.0:
            continue
        # A small geometry margin avoids clipping glyph edges while staying
        # outside the neighboring observed lanes.
        margin = min(3.0, max(1.0, (right_edge - left_edge) * 0.005))
        text_left, text_right = left_edge + margin, right_edge - margin

        row_box = tuple(float(value) for value in row_boxes[row_index])
        row_items = items_by_row[row_index]
        if len(row_items) < 3:
            continue

        # Cluster observed words by baseline center, retaining boxes for a
        # tight local crop. OCR boxes from a single line may vary slightly in y.
        baselines: list[list[tuple[tuple[float, ...], Any]]] = []
        for entry in sorted(row_items, key=lambda pair: (pair[0][1] + pair[0][3]) / 2.0):
            center_y = (entry[0][1] + entry[0][3]) / 2.0
            if baselines:
                prior_y = median((box[1] + box[3]) / 2.0 for box, _ in baselines[-1])
            else:
                prior_y = None
            if prior_y is None or abs(center_y - prior_y) > 3.5:
                baselines.append([entry])
            else:
                baselines[-1].append(entry)

        for baseline_index, baseline in enumerate(baselines):
            centers = [(box[0] + box[2]) / 2.0 for box, _ in baseline]
            left_anchors = [entry for entry in baseline if (entry[0][0] + entry[0][2]) / 2.0 < text_left]
            right_anchors = [entry for entry in baseline if (entry[0][0] + entry[0][2]) / 2.0 > text_right]
            text_words = [
                entry for entry in baseline
                if text_left <= (entry[0][0] + entry[0][2]) / 2.0 <= text_right
            ]
            if len(centers) < 3 or not left_anchors or not right_anchors or text_words:
                continue

            baseline_y = median((box[1] + box[3]) / 2.0 for box, _ in baseline)
            continuation_tokens: set[str] = set()
            for later_baseline in baselines[baseline_index + 1:]:
                later_y = median((box[1] + box[3]) / 2.0 for box, _ in later_baseline)
                if later_y <= baseline_y + 4.0:
                    continue
                for box, item in later_baseline:
                    center_x = (box[0] + box[2]) / 2.0
                    if text_left <= center_x <= text_right:
                        continuation_tokens.update(token_set(item.get("text", "")))
            if len(existing_tokens & continuation_tokens) < min(2, len(existing_tokens)):
                continue

            anchor_boxes = [box for box, _ in baseline]
            crop_box = [
                round(text_left, 3),
                max(row_box[1], min(box[1] for box in anchor_boxes) - 0.5),
                round(text_right, 3),
                min(row_box[3], max(box[3] for box in anchor_boxes) + 0.5),
            ]
            if crop_box[3] - crop_box[1] < 5.0:
                continue

            try:
                from backend.services.ocr_adapter import recognize_ocr_text_line_crop

                reading = recognize_ocr_text_line_crop(page, crop_box)
            except Exception:
                reading = None
            recovered_line = _clean_cell(reading.get("text")) if isinstance(reading, dict) else ""
            try:
                confidence = float(reading.get("confidence")) if isinstance(reading, dict) else 0.0
            except (TypeError, ValueError):
                confidence = 0.0
            recovered_tokens = token_set(recovered_line)
            duplicate_ratio = (
                len(recovered_tokens & existing_tokens) / len(recovered_tokens)
                if recovered_tokens else 1.0
            )
            accepted = bool(recovered_line and confidence >= 0.90 and duplicate_ratio < 0.70)
            raw_text = row[text_column]
            if accepted:
                row[text_column] = _clean_cell(f"{recovered_line} {existing_text}")
                changed = True

                # The same missed baseline can also hide a value in a numeric
                # lane next to the text column. Probe only blank, statistically
                # numeric lanes whose x-position is independently observed in
                # neighboring rows, and accept only a clean high-confidence
                # single-number reading.
                for numeric_column in sorted(numeric_lane_columns):
                    if numeric_column >= len(row) or _clean_cell(row[numeric_column]):
                        continue
                    numeric_lane = observed_lanes.get(numeric_column)
                    if not numeric_lane or len(numeric_lane) < 2:
                        continue
                    lane_boxes = list(numeric_lane.values())
                    lane_left = float(median(box[0] for box in lane_boxes))
                    lane_right = float(median(box[2] for box in lane_boxes))
                    if lane_right <= lane_left:
                        continue
                    lane_padding = min(1.5, max(0.5, (lane_right - lane_left) * 0.04))
                    numeric_crop = [
                        max(float(getattr(getattr(page, "rect", None), "x0", 0.0)), lane_left - lane_padding),
                        crop_box[1],
                        min(float(getattr(getattr(page, "rect", None), "x1", lane_right)), lane_right + lane_padding),
                        crop_box[3],
                    ]
                    try:
                        numeric_reading = recognize_ocr_text_line_crop(page, numeric_crop)
                    except Exception:
                        numeric_reading = None
                    if not isinstance(numeric_reading, dict):
                        continue
                    numeric_text = _clean_cell(numeric_reading.get("text"))
                    try:
                        numeric_confidence = float(numeric_reading.get("confidence"))
                    except (TypeError, ValueError):
                        continue
                    numeric_tokens = re.findall(
                        r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])",
                        numeric_text,
                    )
                    remainder = numeric_text
                    if len(numeric_tokens) == 1:
                        remainder = remainder.replace(numeric_tokens[0], "", 1)
                    if (
                        len(numeric_tokens) != 1 or numeric_confidence < 0.95
                        or re.search(r"[A-Za-z0-9]", remainder)
                        or not _is_numeric_token(numeric_tokens[0])
                    ):
                        continue
                    row[numeric_column] = numeric_tokens[0]
                    cell_flags[row_index][numeric_column] = True
                    numeric_reason = "ocr_numeric_cell_recovered"
                    if numeric_reason not in row_reasons[row_index]:
                        row_reasons[row_index].append(numeric_reason)
                    issues.append({
                        "code": numeric_reason,
                        "message": (
                            f"Nilai numerik kosong dibaca ulang dari crop kolom yang teramati "
                            f"(confidence {numeric_confidence:.0%}); cocokkan dengan PDF asli."
                        ),
                        "row_index": row_index,
                        "column_index": numeric_column,
                        "raw_ocr_value": "",
                        "recovered_value": numeric_tokens[0],
                        "confidence": numeric_confidence,
                        "source_bbox": row_box,
                        "crop_bbox": numeric_crop,
                    })
                    table["review_required"] = True
                    table["validation_status"] = "needs_review"

            while row_index >= len(cell_flags):
                cell_flags.append([False] * column_count)
            while len(cell_flags[row_index]) < column_count:
                cell_flags[row_index].append(False)
            row_flags[row_index] = True
            cell_flags[row_index][text_column] = True
            reason = "ocr_text_line_recovered" if accepted else "possible_missing_ocr_text_line"
            if reason not in row_reasons[row_index]:
                row_reasons[row_index].append(reason)
            issues.append({
                "code": reason,
                "message": (
                    f"Baris teks yang terlewat dibaca ulang dari crop sumber (confidence {confidence:.0%}); "
                    "cocokkan dengan PDF asli."
                    if accepted else
                    "Geometri menunjukkan kemungkinan baris teks terlewat, tetapi crop OCR tidak cukup yakin; "
                    "teks yang sudah terbaca dipertahankan dan baris perlu diperiksa."
                ),
                "row_index": row_index,
                "column_index": text_column,
                "raw_ocr_value": raw_text,
                "recovered_line": recovered_line or None,
                "confidence": confidence if reading else None,
                "source_bbox": row_box,
                "crop_bbox": crop_box,
            })
            table["review_required"] = True
            table["validation_status"] = "needs_review"
            break

    if changed:
        table["row_count"] = len(rows)


def _normalize_confusable_numeric_ocr_cells(page: Any, table: dict[str, Any]) -> None:
    """Correct narrow OCR digit confusions only inside observed numeric columns.

    A candidate column must already contain several clearly numeric values and
    at least 70% of its non-empty cells must be numeric. Each changed value is
    retained verbatim in a source-linked review issue; no value is inferred from
    a document name, header template, or fixed column index.
    """
    if not getattr(page, "ocr_word_count", 0):
        return
    rows = table.get("rows")
    if not isinstance(rows, list) or len(rows) < 5:
        return
    column_count = max(
        int(table.get("column_count") or 0),
        max((len(row) for row in rows if isinstance(row, (list, tuple))), default=0),
    )
    cell_boxes = table.get("cell_source_bboxes") or []
    numeric_columns: set[int] = set()
    for column_index in range(column_count):
        values = [
            _clean_cell(row[column_index])
            for row in rows
            if isinstance(row, (list, tuple))
            and column_index < len(row)
            and _clean_cell(row[column_index])
        ]
        plain_numeric_count = sum(_is_numeric_token(value) for value in values)
        if len(values) >= 5 and plain_numeric_count >= 4 and plain_numeric_count / len(values) >= 0.70:
            numeric_columns.add(column_index)

    normalizations: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        for column_index in numeric_columns:
            if column_index >= len(row):
                continue
            original = _clean_cell(row[column_index])
            recovered = _ocr_numeric_recovery_candidate(original)
            if (
                recovered is None
                and original.strip(_OCR_NUMERIC_EDGE_QUOTES).casefold() == "s"
            ):
                # A standalone OCR ``s`` is a common 5/S shape confusion. Only
                # accept it here, after the surrounding column has independently
                # met the numeric-lane threshold and a source cell bbox exists.
                recovered = "5"
            if recovered is None:
                continue
            bbox = (
                cell_boxes[row_index][column_index]
                if row_index < len(cell_boxes)
                and column_index < len(cell_boxes[row_index])
                else None
            )
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            row[column_index] = recovered
            normalizations.append({
                "code": "ocr_numeric_confusable_normalized",
                "message": (
                    f"Nilai OCR '{original}' dibaca sebagai '{recovered}' karena berada di kolom "
                    "yang dominan numerik; cocokkan perubahan ini dengan PDF sumber."
                ),
                "raw_text": original,
                "normalized_text": recovered,
                "row_index": row_index,
                "column_index": column_index,
                "source_bbox": [round(float(value), 2) for value in bbox],
                "confidence": _ocr_confidence_for_bbox(page, bbox),
            })

    if not normalizations:
        return
    row_flags = table.setdefault("rows_need_review", [False] * len(rows))
    row_reasons = table.setdefault("row_review_reasons", [[] for _ in rows])
    cell_flags = table.setdefault(
        "cell_review_flags", [[False] * column_count for _ in rows]
    )
    while len(row_flags) < len(rows):
        row_flags.append(False)
    while len(row_reasons) < len(rows):
        row_reasons.append([])
    while len(cell_flags) < len(rows):
        cell_flags.append([False] * column_count)
    for item in normalizations:
        row_index, column_index = item["row_index"], item["column_index"]
        while len(cell_flags[row_index]) < column_count:
            cell_flags[row_index].append(False)
        cell_flags[row_index][column_index] = True
        row_flags[row_index] = True
        if "ocr_numeric_confusable_normalized" not in row_reasons[row_index]:
            row_reasons[row_index].append("ocr_numeric_confusable_normalized")
    issues = table.setdefault("review_issues", [])
    for item in normalizations:
        prior_unexpected_text = next((
            issue for issue in issues
            if issue.get("code") == "unexpected_text_in_numeric_lane"
            and issue.get("row_index") == item["row_index"]
            and issue.get("column_index") == item["column_index"]
        ), None)
        if prior_unexpected_text is not None:
            prior_unexpected_text.clear()
            prior_unexpected_text.update(item)
        else:
            issues.append(item)
    table["review_required"] = True
    table["validation_status"] = "needs_review"


def _stamp_numeric_cell_crop_bbox(
    table: dict[str, Any], row_index: int, column_index: int, page: Any = None,
) -> list[float] | None:
    """Estimate the interior of a stamp-hit numeric cell from neighboring rows."""
    rows = table.get("rows") or []
    cell_boxes = table.get("cell_source_bboxes") or []
    row_boxes = table.get("row_source_bboxes") or []
    current_box = (
        cell_boxes[row_index][column_index]
        if row_index < len(cell_boxes)
        and column_index < len(cell_boxes[row_index])
        else None
    )

    def valid_box(box: Any) -> bool:
        return (
            isinstance(box, (list, tuple)) and len(box) == 4
            and float(box[2]) > float(box[0])
            and float(box[3]) > float(box[1])
        )

    if not valid_box(current_box):
        return _stamp_numeric_crop_bbox_from_ocr_lanes(
            page, table, row_index, column_index, valid_box
        )
    centers_by_column: dict[int, list[float]] = {}
    for column in (column_index - 1, column_index, column_index + 1):
        centers = []
        for other_row in range(len(rows)):
            if other_row == row_index or other_row >= len(cell_boxes):
                continue
            if column >= len(cell_boxes[other_row]):
                continue
            box = cell_boxes[other_row][column]
            if valid_box(box):
                centers.append((float(box[0]) + float(box[2])) / 2.0)
        if centers:
            centers_by_column[column] = centers

    if column_index in centers_by_column:
        center_x = float(median(centers_by_column[column_index]))
    else:
        center_x = (float(current_box[0]) + float(current_box[2])) / 2.0
    left_neighbor = centers_by_column.get(column_index - 1, [])
    right_neighbor = centers_by_column.get(column_index + 1, [])
    left = (
        (float(median(left_neighbor)) + center_x) / 2.0
        if left_neighbor else float(current_box[0])
    )
    right = (
        (center_x + float(median(right_neighbor))) / 2.0
        if right_neighbor else float(current_box[2])
    )
    if right <= left:
        return None

    def vertical_center(box: Any) -> float:
        return (float(box[1]) + float(box[3])) / 2.0

    previous_centers = [
        vertical_center(cell_boxes[index][column_index])
        for index in range(min(row_index, len(cell_boxes)) - 1, -1, -1)
        if column_index < len(cell_boxes[index])
        and valid_box(cell_boxes[index][column_index])
    ]
    next_centers = [
        vertical_center(cell_boxes[index][column_index])
        for index in range(row_index + 1, len(cell_boxes))
        if column_index < len(cell_boxes[index])
        and valid_box(cell_boxes[index][column_index])
    ]
    if previous_centers and next_centers:
        previous_y, next_y = previous_centers[0], next_centers[0]
        current_y = (previous_y + next_y) / 2.0
        top = (previous_y + current_y) / 2.0 + 0.4
        bottom = (current_y + next_y) / 2.0 - 0.4
    else:
        row_box = row_boxes[row_index] if row_index < len(row_boxes) else current_box
        if not valid_box(row_box):
            row_box = current_box
        inset = min(2.0, max(0.5, (float(row_box[3]) - float(row_box[1])) * 0.12))
        top, bottom = float(row_box[1]) + inset, float(row_box[3]) - inset
    if bottom <= top:
        return None
    return [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)]


def _stamp_numeric_crop_bbox_from_ocr_lanes(
    page: Any,
    table: dict[str, Any],
    row_index: int,
    column_index: int,
    valid_box: Callable[[Any], bool],
) -> list[float] | None:
    """Infer a missing cell box from repeated word positions in neighboring rows."""
    rows = table.get("rows") or []
    row_boxes = table.get("row_source_bboxes") or []
    items = getattr(page, "items", None)
    if (
        not isinstance(items, list) or row_index >= len(row_boxes)
        or not valid_box(row_boxes[row_index])
    ):
        return None

    def normalize(value: Any) -> str:
        return re.sub(r"\s+", "", _clean_cell(value)).casefold()

    def center_x(box: Any) -> float:
        return (float(box[0]) + float(box[2])) / 2.0

    def center_y(box: Any) -> float:
        return (float(box[1]) + float(box[3])) / 2.0

    headers = table.get("headers") or []
    header_boxes = table.get("header_source_bboxes") or []
    if not header_boxes and valid_box(table.get("header_source_bbox")):
        header_boxes = [table["header_source_bbox"]]
    header_centers: dict[int, float] = {}
    for lane, header in enumerate(headers):
        header_tokens = set(re.findall(r"[a-z0-9]+", _clean_cell(header).casefold()))
        if not header_tokens:
            continue
        matched_words: list[Any] = []
        for word in items:
            word_tokens = set(re.findall(r"[a-z0-9]+", str(word.get("text", "")).casefold()))
            if not (word_tokens & header_tokens):
                continue
            try:
                word_box = [float(word[key]) for key in ("x0", "y0", "x1", "y1")]
            except (KeyError, TypeError, ValueError):
                continue
            if any(
                valid_box(box)
                and word_box[1] <= float(box[3]) + 2.0
                and word_box[3] >= float(box[1]) - 2.0
                for box in header_boxes
            ):
                matched_words.append(word_box)
        if matched_words:
            header_centers[lane] = median(
                (float(box[0]) + float(box[2])) / 2.0 for box in matched_words
            )

    lane_matches: dict[int, list[tuple[float, float, int, float]]] = {}
    for lane in (column_index - 1, column_index, column_index + 1):
        if lane < 0:
            continue
        matches: list[tuple[float, float, int, float]] = []
        for other_row, values in enumerate(rows):
            if (
                other_row == row_index or other_row >= len(row_boxes)
                or not valid_box(row_boxes[other_row])
                or not isinstance(values, (list, tuple)) or lane >= len(values)
            ):
                continue
            expected = normalize(values[lane])
            if not expected:
                continue
            row_box = row_boxes[other_row]
            row_y = center_y(row_box)
            tolerance_y = max(5.0, (float(row_box[3]) - float(row_box[1])) * 0.60)
            candidates: list[tuple[float, float, float, float]] = []
            for word in items:
                if normalize(word.get("text", "")) != expected:
                    continue
                try:
                    word_box = [float(word[key]) for key in ("x0", "y0", "x1", "y1")]
                    confidence = float(word.get("confidence", 1.0))
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    word_box[2] <= word_box[0] or word_box[3] <= word_box[1]
                    or confidence < 0.75
                ):
                    continue
                word_y = center_y(word_box)
                if abs(word_y - row_y) > tolerance_y:
                    continue
                candidates.append((
                    abs(word_y - row_y), center_x(word_box),
                    word_box[2] - word_box[0], confidence,
                ))
            if candidates:
                # One reading per row prevents repeated tokens in a cell from
                # giving a single row extra weight in the geometric lane.
                lane_center = header_centers.get(lane)
                if lane_center is None:
                    _, x, width, confidence = min(candidates, key=lambda value: value[0])
                else:
                    _, x, width, confidence = min(
                        candidates,
                        key=lambda value: abs(value[1] - lane_center) * 4.0 + value[0],
                    )
                matches.append((x, width, other_row, confidence))
        if not matches:
            continue

        tolerance_x = max(6.0, float(getattr(getattr(page, "rect", None), "width", 595.0)) * 0.012)
        clusters: list[list[tuple[float, float, int, float]]] = []
        for match in sorted(matches, key=lambda value: value[0]):
            if clusters and abs(match[0] - median(item[0] for item in clusters[-1])) <= tolerance_x:
                clusters[-1].append(match)
            else:
                clusters.append([match])
        best_cluster = max(
            clusters,
            key=lambda cluster: (len({item[2] for item in cluster}), sum(item[3] for item in cluster)),
        )
        if len({item[2] for item in best_cluster}) >= 2:
            lane_matches[lane] = best_cluster

    target_matches = lane_matches.get(column_index)
    if not target_matches:
        return None
    x_center = float(header_centers.get(
        column_index, median(item[0] for item in target_matches)
    ))
    median_width = float(median(item[1] for item in target_matches))
    left_matches = lane_matches.get(column_index - 1, [])
    right_matches = lane_matches.get(column_index + 1, [])
    if column_index in header_centers:
        lane_padding = max(3.0, median_width * 0.25)
        left, right = x_center - median_width / 2.0 - lane_padding, x_center + median_width / 2.0 + lane_padding
    elif left_matches:
        left = (float(median(item[0] for item in left_matches)) + x_center) / 2.0
    else:
        left = x_center - max(10.0, median_width * 1.5)
    if column_index in header_centers:
        pass
    elif right_matches:
        right = (x_center + float(median(item[0] for item in right_matches))) / 2.0
    else:
        right = x_center + max(10.0, median_width * 1.5)

    current_box = row_boxes[row_index]
    current_y = center_y(current_box)
    source_value = _clean_cell(rows[row_index][column_index]) if column_index < len(rows[row_index]) else ""
    source_tokens = set(re.findall(r"[a-z0-9]+", source_value.casefold()))
    target_words: list[tuple[tuple[float, ...], str, float]] = []
    lane_x_tolerance = max(24.0, median_width * 1.8)
    for word in items:
        word_tokens = set(re.findall(r"[a-z0-9]+", str(word.get("text", "")).casefold()))
        if not (word_tokens & source_tokens):
            continue
        try:
            word_box = tuple(float(word[key]) for key in ("x0", "y0", "x1", "y1"))
            confidence = float(word.get("confidence", 1.0))
        except (KeyError, TypeError, ValueError):
            continue
        if (
            word_box[2] <= word_box[0] or word_box[3] <= word_box[1]
            or abs(center_x(word_box) - x_center) > lane_x_tolerance
            or _bbox_overlap_ratio(word_box, tuple(float(value) for value in current_box)) < 0.10
        ):
            continue
        target_words.append((word_box, str(word.get("text", "")), confidence))
    target_word = max(
        target_words,
        key=lambda item: (
            bool(_is_numeric_token(item[1])), item[2],
            -abs(center_x(item[0]) - x_center), -center_y(item[0]),
        ),
        default=None,
    )
    if target_word is not None:
        target_box = target_word[0]
        target_y = center_y(target_box)
        y_padding = max(7.5, (float(target_box[3]) - float(target_box[1])) * 0.80)
        top, bottom = target_y - y_padding, target_y + y_padding
    else:
        previous = [center_y(box) for box in row_boxes[:row_index] if valid_box(box)]
        following = [center_y(box) for box in row_boxes[row_index + 1:] if valid_box(box)]
        if previous and following:
            top = (previous[-1] + current_y) / 2.0 + 0.25
            bottom = (current_y + following[0]) / 2.0 - 0.25
        else:
            inset = min(2.0, max(0.5, (float(current_box[3]) - float(current_box[1])) * 0.12))
            top, bottom = float(current_box[1]) + inset, float(current_box[3]) - inset

    rect = getattr(page, "rect", None)
    if rect is not None:
        left = max(float(getattr(rect, "x0", 0.0)), left)
        right = min(float(getattr(rect, "x1", getattr(rect, "width", right))), right)
        top = max(float(getattr(rect, "y0", 0.0)), top)
        bottom = min(float(getattr(rect, "y1", getattr(rect, "height", bottom))), bottom)
    if right <= left or bottom <= top or right - left > 160.0 or bottom - top > 45.0:
        return None
    return [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)]


def _read_stamp_occluded_numeric_value(
    page: Any, table: dict[str, Any], row_index: int, column_index: int,
) -> dict[str, Any] | None:
    crop_bbox = _stamp_numeric_cell_crop_bbox(table, row_index, column_index, page)
    if crop_bbox is None:
        return None
    try:
        from backend.services.ocr_adapter import recognize_stamp_occluded_numeric_crop

        reading = recognize_stamp_occluded_numeric_crop(page, crop_bbox)
    except Exception:
        return None
    if not isinstance(reading, dict):
        return None
    raw_text = _clean_cell(reading.get("text"))
    try:
        confidence = float(reading.get("confidence"))
    except (TypeError, ValueError):
        return None
    candidate = raw_text if _is_numeric_token(raw_text) else _ocr_numeric_recovery_candidate(raw_text)
    agreement_count = 1
    if candidate is None or confidence < 0.90:
        variant_candidates: dict[str, list[float]] = {}
        for variant in reading.get("variant_readings", []):
            if not isinstance(variant, dict):
                continue
            variant_text = _clean_cell(variant.get("text"))
            variant_candidate = (
                variant_text if _is_numeric_token(variant_text)
                else _ocr_numeric_recovery_candidate(variant_text)
            )
            try:
                variant_confidence = float(variant.get("confidence"))
            except (TypeError, ValueError):
                continue
            if variant_candidate is not None and variant_confidence >= 0.55:
                variant_candidates.setdefault(variant_candidate, []).append(variant_confidence)
        consensus = max(variant_candidates.items(), key=lambda item: len(item[1]), default=None)
        if consensus is None or len(consensus[1]) < 2:
            return None
        candidate = consensus[0]
        confidence = min(consensus[1])
        agreement_count = len(consensus[1])
    return {
        "text": candidate,
        "confidence": confidence,
        "crop_bbox": crop_bbox,
        "agreement_count": agreement_count,
    }


def _annotate_colored_stamp_overlaps(page: Any, table: dict[str, Any]) -> None:
    """Flag OCR cells under possible colored seals, using row geometry as fallback."""
    get_stamp_regions = getattr(page, "get_stamp_regions", None)
    if not callable(get_stamp_regions):
        return
    rows = table.get("rows")
    if not isinstance(rows, list) or not rows:
        return
    try:
        stamp_regions = get_stamp_regions()
    except Exception:  # optional image analysis must not fail table extraction
        return
    if not stamp_regions:
        return

    column_count = max(
        int(table.get("column_count") or 0),
        max((len(row) for row in rows if isinstance(row, (list, tuple))), default=0),
    )
    cell_boxes = table.get("cell_source_bboxes") or []
    row_boxes = table.get("row_source_bboxes") or []
    row_flags = table.setdefault("rows_need_review", [False] * len(rows))
    row_reasons = table.setdefault("row_review_reasons", [[] for _ in rows])
    cell_flags = table.setdefault(
        "cell_review_flags", [[False] * column_count for _ in rows]
    )
    issues = table.setdefault("review_issues", [])
    numeric_lane_columns: set[int] = set()
    for column_index in range(column_count):
        values = [
            _clean_cell(row[column_index])
            for row in rows
            if isinstance(row, (list, tuple))
            and column_index < len(row)
            and _clean_cell(row[column_index])
        ]
        numeric_count = sum(_is_numeric_token(value) for value in values)
        if len(values) >= 5 and numeric_count >= 4 and numeric_count / len(values) >= 0.70:
            numeric_lane_columns.add(column_index)

    for row_index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            continue
        for column_index in range(min(column_count, len(row))):
            cell_bbox = (
                cell_boxes[row_index][column_index]
                if row_index < len(cell_boxes)
                and column_index < len(cell_boxes[row_index])
                else None
            )
            if not isinstance(cell_bbox, (list, tuple)) or len(cell_bbox) != 4:
                continue
            for stamp_bbox in stamp_regions:
                try:
                    overlap = _bbox_overlap_ratio(
                        tuple(float(value) for value in cell_bbox),
                        tuple(float(value) for value in stamp_bbox),
                    )
                except (TypeError, ValueError):
                    continue
                if overlap < 0.20:
                    continue
                raw_value = _clean_cell(row[column_index])
                prior_numeric_recovery = next((
                    issue for issue in issues
                    if issue.get("code") == "ocr_numeric_confusable_normalized"
                    and issue.get("row_index") == row_index
                    and issue.get("column_index") == column_index
                ), None)
                prior_cell_issue = next((
                    issue for issue in issues
                    if issue.get("code") in {
                        "unexpected_text_in_numeric_lane",
                        "ocr_numeric_confusable_normalized",
                    }
                    and issue.get("row_index") == row_index
                    and issue.get("column_index") == column_index
                ), None)
                suspicious_text = (
                    bool(re.search(r"[A-Za-zÀ-ž]", raw_value))
                    or prior_numeric_recovery is not None
                )
                attempt_recovery = column_index in numeric_lane_columns and suspicious_text
                recovered_reading = (
                    _read_stamp_occluded_numeric_value(page, table, row_index, column_index)
                    if attempt_recovery else None
                )
                cleared_value = attempt_recovery and recovered_reading is None
                if attempt_recovery:
                    raw_value = str(
                        prior_numeric_recovery.get("raw_text")
                        if prior_numeric_recovery else raw_value
                    )
                    if isinstance(row, tuple):
                        row = list(row)
                        rows[row_index] = row
                    if recovered_reading is not None:
                        row[column_index] = recovered_reading["text"]
                    else:
                        row[column_index] = ""
                    if prior_numeric_recovery is not None and recovered_reading is None:
                        prior_numeric_recovery["superseded_by_stamp_review"] = True
                        prior_numeric_recovery["message"] = (
                            "Koreksi karakter OCR dibatalkan karena sel bertumpang-tindih dengan kemungkinan cap; "
                            "cocokkan nilai asli dengan PDF sumber."
                        )
                    elif prior_numeric_recovery is not None:
                        prior_numeric_recovery["superseded_by_stamp_reread"] = True
                        prior_numeric_recovery["message"] = (
                            f"Nilai terbaca ulang sebagai '{recovered_reading['text']}' setelah tinta cap dan garis "
                            "tabel ditekan; tetap cocokkan dengan PDF sumber."
                        )
                while row_index >= len(cell_flags):
                    cell_flags.append([False] * column_count)
                while len(cell_flags[row_index]) < column_count:
                    cell_flags[row_index].append(False)
                cell_flags[row_index][column_index] = True
                row_flags[row_index] = True
                if "possible_colored_stamp_overlap" not in row_reasons[row_index]:
                    row_reasons[row_index].append("possible_colored_stamp_overlap")
                stamp_issue = {
                    "code": "possible_colored_stamp_overlap",
                    "message": (
                        (
                            f"Kemungkinan cap menimpa sel. Crop sel dibaca ulang sebagai "
                            f"'{recovered_reading['text']}' (confidence {recovered_reading['confidence']:.0%}); "
                            f"OCR awal {raw_value!r} tetap tercatat. Cocokkan hasil dengan PDF sumber."
                        ) if recovered_reading is not None else
                        (
                            f"Kemungkinan cap berwarna menimpa sel pada lane angka; teks OCR {raw_value!r} "
                            "dikeluarkan dari nilai final dan disimpan di detail indikator. Cocokkan angka "
                            "asli dengan PDF sumber."
                        ) if cleared_value else
                        "Kemungkinan cap berwarna menimpa sel ini. Nilai OCR dipertahankan, tetapi cocokkan dengan PDF sumber."
                    ),
                    "row_index": row_index,
                    "column_index": column_index,
                    "value": raw_value,
                    "raw_ocr_value": raw_value,
                    "value_cleared": cleared_value,
                    "value_recovered": recovered_reading is not None,
                    "recovered_value": recovered_reading["text"] if recovered_reading else None,
                    "recovery_confidence": recovered_reading["confidence"] if recovered_reading else None,
                    "recovery_bbox": recovered_reading["crop_bbox"] if recovered_reading else None,
                    "source_bbox": list(cell_bbox),
                    "stamp_bbox": list(stamp_bbox),
                    "overlap_ratio": round(overlap, 4),
                }
                if attempt_recovery and prior_cell_issue is not None:
                    # The final cell value and its warning now describe the
                    # stamp-aware crop read. Replace the stale generic numeric
                    # lane warning so the UI does not imply that the recovered
                    # cell is still blank or still contains fused OCR text.
                    prior_cell_issue.clear()
                    prior_cell_issue.update(stamp_issue)
                    if not any(
                        issue.get("row_index") == row_index
                        and issue.get("code") == "unexpected_text_in_numeric_lane"
                        for issue in issues
                    ):
                        row_reasons[row_index] = [
                            reason for reason in row_reasons[row_index]
                            if reason != "unexpected_text_in_numeric_lane"
                        ]
                else:
                    issues.append(stamp_issue)
                break

        # Some OCR reconstruction strategies publish row boxes but no cell boxes.
        # In that case, use overlapping OCR words to associate a stamp with a
        # numeric-dominant output column instead of silently skipping the row.
        row_cell_boxes = cell_boxes[row_index] if row_index < len(cell_boxes) else []
        missing_cell_columns = {
            column_index
            for column_index in range(min(column_count, len(row)))
            if column_index >= len(row_cell_boxes)
            or not isinstance(row_cell_boxes[column_index], (list, tuple))
            or len(row_cell_boxes[column_index]) != 4
        }
        if not missing_cell_columns:
            continue
        row_bbox = row_boxes[row_index] if row_index < len(row_boxes) else None
        if not isinstance(row_bbox, (list, tuple)) or len(row_bbox) != 4:
            continue
        for stamp_bbox in stamp_regions:
            try:
                row_overlap = _bbox_overlap_ratio(
                    tuple(float(value) for value in row_bbox),
                    tuple(float(value) for value in stamp_bbox),
                )
            except (TypeError, ValueError):
                continue
            if row_overlap < 0.20:
                continue

            stamp_tokens: set[str] = set()
            for word in getattr(page, "items", []):
                try:
                    word_bbox = tuple(float(word[key]) for key in ("x0", "y0", "x1", "y1"))
                    word_text = str(word.get("text", "")).strip()
                    in_stamp = _bbox_overlap_ratio(word_bbox, tuple(stamp_bbox)) >= 0.15
                    in_row = _bbox_overlap_ratio(word_bbox, tuple(row_bbox)) >= 0.15
                except (KeyError, TypeError, ValueError):
                    continue
                if in_stamp and in_row:
                    stamp_tokens.update(re.findall(r"[a-z0-9]+", word_text.casefold()))

            row_flags[row_index] = True
            if "possible_colored_stamp_overlap" not in row_reasons[row_index]:
                row_reasons[row_index].append("possible_colored_stamp_overlap")
            matched_columns: set[int] = set()
            for column_index in numeric_lane_columns & missing_cell_columns:
                if column_index >= len(row):
                    continue
                raw_value = _clean_cell(row[column_index])
                value_tokens = set(re.findall(r"[a-z0-9]+", raw_value.casefold()))
                compact_value = "".join(re.findall(r"[a-z0-9]+", raw_value.casefold()))
                word_match = bool(value_tokens & stamp_tokens) or any(
                    len(token) >= 3 and token in compact_value
                    for token in stamp_tokens
                )
                if not word_match:
                    continue
                matched_columns.add(column_index)
                prior_numeric_recovery = next((
                    issue for issue in issues
                    if issue.get("code") == "ocr_numeric_confusable_normalized"
                    and issue.get("row_index") == row_index
                    and issue.get("column_index") == column_index
                ), None)
                suspicious_text = (
                    bool(re.search(r"[A-Za-zÀ-ž]", raw_value))
                    or prior_numeric_recovery is not None
                )
                recovered_reading = (
                    _read_stamp_occluded_numeric_value(page, table, row_index, column_index)
                    if suspicious_text else None
                )
                cleared_value = suspicious_text and recovered_reading is None
                prior_cell_issue = next((
                    issue for issue in issues
                    if issue.get("code") in {
                        "unexpected_text_in_numeric_lane",
                        "ocr_numeric_confusable_normalized",
                    }
                    and issue.get("row_index") == row_index
                    and issue.get("column_index") == column_index
                ), None)
                if suspicious_text:
                    raw_value = str(
                        prior_numeric_recovery.get("raw_text")
                        if prior_numeric_recovery else raw_value
                    )
                    if isinstance(row, tuple):
                        row = list(row)
                        rows[row_index] = row
                    row[column_index] = recovered_reading["text"] if recovered_reading else ""
                    if prior_numeric_recovery is not None:
                        prior_numeric_recovery["superseded_by_stamp_review"] = recovered_reading is None
                        prior_numeric_recovery["message"] = (
                            (
                                f"Crop sel dibaca ulang sebagai '{recovered_reading['text']}'; cocokkan nilai "
                                "dengan PDF sumber."
                            ) if recovered_reading else
                            "Koreksi karakter OCR dibatalkan karena kata sumber bertumpang-tindih dengan kemungkinan cap; "
                            "cocokkan nilai asli dengan PDF sumber."
                        )
                while row_index >= len(cell_flags):
                    cell_flags.append([False] * column_count)
                while len(cell_flags[row_index]) < column_count:
                    cell_flags[row_index].append(False)
                cell_flags[row_index][column_index] = True
                stamp_issue = {
                    "code": "possible_colored_stamp_overlap",
                    "message": (
                        (
                            f"Crop numerik dibaca ulang sebagai '{recovered_reading['text']}' "
                            f"(confidence {recovered_reading['confidence']:.0%}); OCR awal {raw_value!r} "
                            "tetap tersimpan. Cocokkan dengan PDF sumber."
                        ) if recovered_reading else
                        f"Kata OCR pada area cap cocok dengan sel lane angka ({raw_value!r}); "
                        + (
                            "teks dikeluarkan dari nilai final dan dipertahankan pada indikator. "
                            if cleared_value else "nilai dipertahankan tetapi harus diperiksa. "
                        )
                        + "Geometri sel tidak tersedia, jadi posisi sel ditautkan melalui baris dan kata OCR sumber."
                    ),
                    "row_index": row_index,
                    "column_index": column_index,
                    "value": raw_value,
                    "raw_ocr_value": raw_value,
                    "value_cleared": cleared_value,
                    "value_recovered": recovered_reading is not None,
                    "recovered_value": recovered_reading["text"] if recovered_reading else None,
                    "recovery_confidence": recovered_reading["confidence"] if recovered_reading else None,
                    "recovery_agreement_count": recovered_reading.get("agreement_count") if recovered_reading else None,
                    "recovery_bbox": recovered_reading["crop_bbox"] if recovered_reading else None,
                    "source_bbox": list(row_bbox),
                    "stamp_bbox": list(stamp_bbox),
                    "geometry_fallback": "row_and_overlapping_words",
                    "overlap_ratio": round(row_overlap, 4),
                }
                if suspicious_text and prior_cell_issue is not None:
                    prior_cell_issue.clear()
                    prior_cell_issue.update(stamp_issue)
                    if not any(
                        issue.get("row_index") == row_index
                        and issue.get("code") == "unexpected_text_in_numeric_lane"
                        for issue in issues
                    ):
                        row_reasons[row_index] = [
                            reason for reason in row_reasons[row_index]
                            if reason != "unexpected_text_in_numeric_lane"
                        ]
                else:
                    issues.append(stamp_issue)

            if not matched_columns:
                issues.append({
                    "code": "possible_colored_stamp_overlap",
                    "message": (
                        "Kemungkinan cap berwarna menimpa baris ini, tetapi kotak sel tidak tersedia dan "
                        "teks cap tidak dapat dipasangkan dengan satu lane secara aman; periksa seluruh baris."
                    ),
                    "row_index": row_index,
                    "source_bbox": list(row_bbox),
                    "stamp_bbox": list(stamp_bbox),
                    "geometry_fallback": "row_only",
                    "overlap_ratio": round(row_overlap, 4),
                })
            break

    if any(issue.get("code") == "possible_colored_stamp_overlap" for issue in issues):
        table["review_required"] = True
        table["validation_status"] = "needs_review"


def _clear_non_numeric_text_from_ocr_numeric_lanes(table: dict[str, Any]) -> None:
    """Keep alphabetic-only OCR noise out of statistically numeric scan cells.

    Original OCR text remains attached to a review issue. This does not infer a
    replacement digit and does not use a header name or fixed column index.
    """
    rows = table.get("rows")
    if not isinstance(rows, list) or len(rows) < 5:
        return
    column_count = max(
        int(table.get("column_count") or 0),
        max((len(row) for row in rows if isinstance(row, (list, tuple))), default=0),
    )
    numeric_columns: set[int] = set()
    for column_index in range(column_count):
        values = [
            _clean_cell(row[column_index])
            for row in rows
            if isinstance(row, (list, tuple))
            and column_index < len(row)
            and _clean_cell(row[column_index])
        ]
        numeric_count = sum(_is_numeric_token(value) for value in values)
        if len(values) >= 5 and numeric_count >= 4 and numeric_count / len(values) >= 0.70:
            numeric_columns.add(column_index)
    if not numeric_columns:
        return

    row_flags = table.setdefault("rows_need_review", [False] * len(rows))
    row_reasons = table.setdefault("row_review_reasons", [[] for _ in rows])
    cell_flags = table.setdefault(
        "cell_review_flags", [[False] * column_count for _ in rows]
    )
    issues = table.setdefault("review_issues", [])
    cell_boxes = table.get("cell_source_bboxes") or []
    row_boxes = table.get("row_source_bboxes") or []
    changed = False
    for row_index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            continue
        for column_index in numeric_columns:
            if column_index >= len(row):
                continue
            raw_value = _clean_cell(row[column_index])
            if not re.fullmatch(r"[A-Za-zÀ-ž]+", raw_value):
                continue
            if any(
                issue.get("row_index") == row_index
                and issue.get("column_index") == column_index
                and issue.get("code") == "possible_colored_stamp_overlap"
                for issue in issues
            ):
                continue
            if isinstance(row, tuple):
                row = list(row)
                rows[row_index] = row
            row[column_index] = ""
            row_flags[row_index] = True
            if "nonnumeric_text_cleared_from_numeric_lane" not in row_reasons[row_index]:
                row_reasons[row_index].append("nonnumeric_text_cleared_from_numeric_lane")
            while row_index >= len(cell_flags):
                cell_flags.append([False] * column_count)
            while len(cell_flags[row_index]) < column_count:
                cell_flags[row_index].append(False)
            cell_flags[row_index][column_index] = True
            bbox = (
                cell_boxes[row_index][column_index]
                if row_index < len(cell_boxes)
                and column_index < len(cell_boxes[row_index])
                else row_boxes[row_index] if row_index < len(row_boxes) else None
            )
            issues.append({
                "code": "nonnumeric_text_cleared_from_numeric_lane",
                "message": (
                    f"Teks OCR {raw_value!r} bukan angka pada lane yang secara konsisten numerik. "
                    "Sel dikosongkan tanpa menebak digit; teks mentah tersedia untuk pemeriksaan sumber."
                ),
                "row_index": row_index,
                "column_index": column_index,
                "raw_ocr_value": raw_value,
                "value_cleared": True,
                "source_bbox": list(bbox) if isinstance(bbox, (list, tuple)) else None,
            })
            changed = True
    if changed:
        table["review_required"] = True
        table["validation_status"] = "needs_review"


def _parse_arithmetic_numeric_cell(value: Any) -> Decimal | None:
    """Parse an unambiguous OCR number for cross-row consistency checks."""
    text = _clean_cell(value).replace("−", "-").replace(" ", "")
    if not text or not re.fullmatch(r"[()+\-]?[\d,.]+[)]?", text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if "," in text and "." not in text:
        groups = text.lstrip("+-").split(",")
        if len(groups) > 1 and all(len(group) == 3 for group in groups[1:]):
            text = text.replace(",", "")
        else:
            # A comma can mean either a decimal marker or digit grouping.
            # Leave that locale-sensitive representation for human review.
            return None
    elif "," in text and "." in text:
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if negative:
        number = -abs(number)
    return number if number.is_finite() else None


def _format_recovered_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def _recover_missing_numeric_cells_from_repeated_table_arithmetic(
    table: dict[str, Any],
) -> None:
    """Recover a missing numeric cell only when other rows prove a stable formula.

    This discovers numeric column relationships from the table body rather than
    relying on column names or fixed layouts. The recovered value remains marked
    for review and is never created without a matching OCR/geometry warning.
    """
    rows = table.get("rows")
    if not isinstance(rows, list) or len(rows) < 8:
        return
    column_count = max(
        int(table.get("column_count") or 0),
        max((len(row) for row in rows if isinstance(row, (list, tuple))), default=0),
    )
    values = [
        [
            _parse_arithmetic_numeric_cell(row[column]) if column < len(row) else None
            for column in range(column_count)
        ]
        for row in rows
    ]
    relationships: list[dict[str, Any]] = []
    for left_column in range(column_count):
        for right_column in range(left_column + 1, column_count):
            for product_column in range(column_count):
                if product_column in {left_column, right_column}:
                    continue
                complete = [
                    row_index for row_index, row_values in enumerate(values)
                    if row_values[left_column] is not None
                    and row_values[right_column] is not None
                    and row_values[product_column] is not None
                ]
                if len(complete) < 8:
                    continue
                supported = []
                for row_index in complete:
                    expected = values[row_index][left_column] * values[row_index][right_column]
                    actual = values[row_index][product_column]
                    tolerance = max(Decimal("0.02"), abs(actual) * Decimal("0.0005"))
                    if abs(expected - actual) <= tolerance:
                        supported.append(row_index)
                support_ratio = len(supported) / len(complete)
                if len(supported) >= 8 and support_ratio >= 0.95:
                    relationships.append({
                        "left": left_column,
                        "right": right_column,
                        "product": product_column,
                        "supporting_rows": supported,
                        "complete_rows": len(complete),
                        "support_ratio": support_ratio,
                    })
    if not relationships:
        return

    issues = table.setdefault("review_issues", [])
    row_flags = table.setdefault("rows_need_review", [False] * len(rows))
    row_reasons = table.setdefault("row_review_reasons", [[] for _ in rows])
    cell_flags = table.setdefault(
        "cell_review_flags", [[False] * column_count for _ in rows]
    )
    recovered_any = False
    for row_index, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        for target_column in range(min(column_count, len(row))):
            if _clean_cell(row[target_column]):
                continue
            cell_issues = [
                issue for issue in issues
                if issue.get("row_index") == row_index
                and issue.get("column_index") == target_column
                and issue.get("code") in {
                    "possible_colored_stamp_overlap",
                    "possible_missing_numeric_cell",
                    "unexpected_text_in_numeric_lane",
                    "nonnumeric_text_cleared_from_numeric_lane",
                    "ocr_numeric_confusable_normalized",
                    "ocr_text_not_assigned_to_cell",
                }
            ]
            if not cell_issues:
                continue
            candidates: dict[Decimal, list[dict[str, Any]]] = {}
            for relationship in relationships:
                left_column = relationship["left"]
                right_column = relationship["right"]
                product_column = relationship["product"]
                if target_column not in {left_column, right_column, product_column}:
                    continue
                if target_column == product_column:
                    left_value = values[row_index][left_column]
                    right_value = values[row_index][right_column]
                    if left_value is None or right_value is None:
                        continue
                    recovered = left_value * right_value
                elif target_column == left_column:
                    other_value = values[row_index][right_column]
                    product_value = values[row_index][product_column]
                    if other_value in {None, Decimal(0)} or product_value is None:
                        continue
                    recovered = product_value / other_value
                else:
                    other_value = values[row_index][left_column]
                    product_value = values[row_index][product_column]
                    if other_value in {None, Decimal(0)} or product_value is None:
                        continue
                    recovered = product_value / other_value
                candidates.setdefault(recovered, []).append(relationship)
            if len(candidates) != 1:
                continue
            recovered_value, evidence = next(iter(candidates.items()))
            # Zero-valued amount cells are often represented by a dash or a
            # blank in the source. Arithmetic alone cannot distinguish those
            # conventions, so preserve the missing cell for manual review.
            if recovered_value == 0:
                continue
            # The formula must independently predict the same target from at
            # least one pair of observed inputs in this row.
            relationship = max(
                evidence,
                key=lambda item: (item["support_ratio"], len(item["supporting_rows"])),
            )
            recovered_text = _format_recovered_decimal(recovered_value)
            row[target_column] = recovered_text
            values[row_index][target_column] = recovered_value
            while len(row_flags) <= row_index:
                row_flags.append(False)
            while len(row_reasons) <= row_index:
                row_reasons.append([])
            while len(cell_flags) <= row_index:
                cell_flags.append([False] * column_count)
            while len(cell_flags[row_index]) < column_count:
                cell_flags[row_index].append(False)
            row_flags[row_index] = True
            cell_flags[row_index][target_column] = True
            reason = "numeric_value_recovered_from_repeated_table_relation"
            if reason not in row_reasons[row_index]:
                row_reasons[row_index].append(reason)

            stale_numeric_issues = [
                issue for issue in cell_issues
                if issue.get("code") != "possible_colored_stamp_overlap"
            ]
            issue = next(
                (item for item in cell_issues if item.get("code") == "possible_colored_stamp_overlap"),
                cell_issues[0],
            )
            issue.update({
                "message": (
                    f"Nilai kosong dipulihkan sebagai '{recovered_text}' dari relasi numerik "
                    f"yang berulang pada {len(relationship['supporting_rows'])}/"
                    f"{relationship['complete_rows']} baris pembanding. Nilai tetap perlu "
                    "dicocokkan dengan PDF sumber."
                ),
                "value_recovered": True,
                "recovered_value": recovered_text,
                "value_cleared": False,
                "recovery_method": "repeated_table_arithmetic",
                "recovery_columns": [
                    relationship["left"], relationship["right"], relationship["product"],
                ],
                "supporting_rows": len(relationship["supporting_rows"]),
                "complete_rows": relationship["complete_rows"],
                "support_ratio": round(relationship["support_ratio"], 4),
            })
            if issue.get("code") != "possible_colored_stamp_overlap":
                issue["code"] = "numeric_value_recovered_from_repeated_table_relation"
            issues[:] = [item for item in issues if item not in stale_numeric_issues]
            if not any(
                item.get("row_index") == row_index
                and item.get("code") == "unexpected_text_in_numeric_lane"
                for item in issues
            ):
                row_reasons[row_index] = [
                    reason for reason in row_reasons[row_index]
                    if reason != "unexpected_text_in_numeric_lane"
                ]
            recovered_any = True

    if recovered_any:
        table["review_required"] = True
        table["validation_status"] = "needs_review"


def _bbox_union(boxes: list[Any]) -> list[float] | None:
    valid: list[tuple[float, float, float, float]] = []
    for box in boxes:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            values = tuple(float(value) for value in box)
        except (TypeError, ValueError):
            continue
        if values[2] > values[0] and values[3] > values[1]:
            valid.append(values)
    if not valid:
        return None
    return [
        round(min(box[0] for box in valid), 2),
        round(min(box[1] for box in valid), 2),
        round(max(box[2] for box in valid), 2),
        round(max(box[3] for box in valid), 2),
    ]


def _merge_adjacent_header_fragments(
    headers: list[str],
    primary_words_by_column: list[list[dict[str, Any]]],
    primary_line: dict[str, Any],
    lines: list[dict[str, Any]],
    lane_boundaries: list[float],
    page_width: float,
) -> tuple[list[str], list[list[list[float]]]]:
    """Attach short, x-aligned header fragments from neighboring baselines."""
    merged = list(headers)
    attached_boxes: list[list[list[float]]] = [[] for _ in headers]
    primary_spans = [
        (
            min(float(word["x0"]) for word in words),
            max(float(word["x1"]) for word in words),
        ) if words else None
        for words in primary_words_by_column
    ]
    primary_y = float(primary_line["y"])
    for line in sorted(lines, key=lambda item: float(item["y"])):
        delta = float(line["y"]) - primary_y
        if delta == 0 or delta < -14.0 or delta > 15.0 or abs(delta) < 2.5:
            continue
        if _is_repeated_identifier_data_line(line, lines, page_width):
            continue
        for run in line.get("runs", []):
            words = list(run.get("words", [])) or [run]
            for word in words:
                text = _clean_cell(word.get("text", "")).strip(" _-:()")
                if not text or len(text) > 24 or not re.search(r"[A-Za-zÀ-ž]", text):
                    continue
                if _is_numeric_token(text):
                    continue
                center = (float(word["x0"]) + float(word["x1"])) / 2.0
                column = bisect_right(lane_boundaries, center)
                if column >= len(merged) or not merged[column] or not primary_spans[column]:
                    continue
                primary_x0, primary_x1 = primary_spans[column]
                fragment_x0, fragment_x1 = float(word["x0"]), float(word["x1"])
                overlap = max(0.0, min(primary_x1, fragment_x1) - max(primary_x0, fragment_x0))
                smaller_width = min(primary_x1 - primary_x0, fragment_x1 - fragment_x0)
                if smaller_width <= 0 or overlap / smaller_width < 0.35:
                    continue
                if re.search(rf"(?<!\w){re.escape(text)}(?!\w)", merged[column], re.IGNORECASE):
                    continue
                merged[column] = (
                    f"{text} {merged[column]}" if delta < 0
                    else f"{merged[column]} ({text})"
                )
                attached_boxes[column].append([
                    round(float(word["x0"]), 2),
                    round(float(word["y0"]), 2),
                    round(float(word["x1"]), 2),
                    round(float(word["y1"]), 2),
                ])
    return merged, attached_boxes


def _extract_repeated_identifier_table(
    page: Any,
    lines: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float | None]:
    """Recover OCR tables from repeated item rows when header anchoring fails.

    The fallback is geometric rather than vendor/schema-specific: it looks for
    several aligned rows that begin with a compact identifier and end in a
    repeated right-side numeric lane. Header labels are copied from positioned
    text immediately above the detected rows whenever the page contains them.
    """
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    if not lines or page_width <= 0 or page_height <= 0:
        return None, None

    candidates: list[dict[str, Any]] = []
    for line in lines:
        runs = sorted(line.get("runs", []), key=lambda item: float(item.get("x0", 0)))
        if len(runs) < 3:
            continue
        # The row identifier needs to be a standalone, compact run in the
        # left portion of the page, with descriptive content to its right.
        identifier_index = next((
            index for index, run in enumerate(runs[:-2])
            if float(run["x0"]) <= page_width * 0.42
            and _is_item_identifier(str(run.get("text", "")))
        ), None)
        if identifier_index is None:
            continue
        identifier = runs[identifier_index]
        identifier_text = _clean_cell(identifier.get("text", ""))
        right_numeric = [
            (index, run) for index, run in enumerate(runs[identifier_index + 2:], identifier_index + 2)
            if _is_numeric_geometry_token(_clean_cell(run.get("text", "")))
            and float(run["x0"]) >= max(page_width * 0.54, float(identifier["x0"]) + page_width * 0.32)
        ]
        if not right_numeric:
            continue
        quantity_index, quantity = max(right_numeric, key=lambda entry: float(entry[1]["x0"]))
        description_runs = [
            run for run in runs[identifier_index + 1:quantity_index]
            if re.search(r"[A-Za-zÀ-ž]", _clean_cell(run.get("text", "")))
        ]
        description = " ".join(_clean_cell(run.get("text", "")) for run in description_runs).strip()
        quantity_text = _clean_cell(quantity.get("text", ""))
        if not description or not quantity_text:
            continue
        candidates.append({
            "y": float(line["y"]),
            "line": line,
            "identifier": identifier,
            "identifier_text": identifier_text,
            "description_runs": description_runs,
            "description": re.sub(r"\s+", " ", description),
            "quantity": quantity,
            "quantity_text": quantity_text,
            "id_x": float(identifier["x0"]),
            "description_x": float(description_runs[0]["x0"]),
            "quantity_x": float(quantity["x0"]),
            "runs": runs,
        })

    if len(candidates) < 2:
        return None, None
    candidates.sort(key=lambda item: item["y"])
    repeated_x_slope = _estimate_ocr_column_x_slope(lines, page_width)

    # Split rows into local vertical groups so unrelated page text cannot join
    # a table. Page-scale limits are used because scanned pages are normalized
    # to PDF points before entering this function.
    groups: list[list[dict[str, Any]]] = []
    max_vertical_gap = max(30.0, min(48.0, page_height * 0.055))
    for candidate in candidates:
        if not groups:
            groups.append([candidate])
            continue
        previous = groups[-1][-1]
        aligned = (
            abs(candidate["id_x"] - previous["id_x"]) <= 18.0
            and abs(candidate["quantity_x"] - previous["quantity_x"]) <= 28.0
        )
        if candidate["y"] - previous["y"] <= max_vertical_gap and aligned:
            groups[-1].append(candidate)
        else:
            groups.append([candidate])

    # A valid sequence should contain multiple separate row identifiers and
    # consistently aligned first/last columns. Prefer the largest such group.
    viable: list[tuple[list[dict[str, Any]], list[dict[str, Any]], set[int]]] = []
    for group in groups:
        if len(group) < 2:
            continue
        unique_ids = {item["identifier_text"].casefold() for item in group}
        if len(unique_ids) < 2:
            continue
        reference_y = float(median(item["y"] for item in group))
        aligned_id_x = [
            item["id_x"] - repeated_x_slope * (item["y"] - reference_y)
            for item in group
        ]
        aligned_qty_x = [
            item["quantity_x"] - repeated_x_slope * (item["y"] - reference_y)
            for item in group
        ]
        id_x = float(median(aligned_id_x))
        qty_x = float(median(aligned_qty_x))
        aligned_rows = [
            item for item in group
            if abs(item["id_x"] - repeated_x_slope * (item["y"] - reference_y) - id_x) <= 12.0
            and abs(item["quantity_x"] - repeated_x_slope * (item["y"] - reference_y) - qty_x) <= 22.0
        ]
        if len(aligned_rows) >= 2:
            viable.append((aligned_rows, group, {id(item) for item in aligned_rows}))
    if not viable:
        return None, None
    _, rows_found, aligned_row_ids = max(
        viable,
        key=lambda candidate: (
            len(candidate[0]),
            candidate[0][-1]["y"] - candidate[0][0]["y"],
        ),
    )

    id_x = float(median(item["id_x"] for item in rows_found))
    qty_x = float(median(item["quantity_x"] for item in rows_found))
    if not id_x < qty_x:
        return None, None

    # A missing header must not collapse every interior lane into the
    # description. Recover lane starts from repeated OCR run positions. First
    # compensate for page skew using the same repeated-ID evidence as the
    # header-anchored path, then retain only starts observed on multiple rows.
    reference_y = float(median(item["y"] for item in rows_found))
    x_slope = repeated_x_slope
    lane_observations: list[tuple[float, int]] = []
    adjacent_value_lanes: list[tuple[float, float, int]] = []
    for row_index, row in enumerate(rows_found):
        for run in row["runs"]:
            normalized_x = float(run["x0"]) - x_slope * (float(row["y"]) - reference_y)
            lane_observations.append((normalized_x, row_index))
            words = sorted(run.get("words", []), key=lambda word: float(word["x0"]))
            if len(words) != 2:
                continue
            numeric_words = [word for word in words if _is_numeric_token(str(word.get("text", "")))]
            text_words = [word for word in words if not _is_numeric_token(str(word.get("text", "")))]
            if len(numeric_words) != 1 or len(text_words) != 1:
                continue
            numeric_word, text_word = numeric_words[0], text_words[0]
            numeric_x = float(numeric_word["x0"]) - x_slope * (float(row["y"]) - reference_y)
            text_x = float(text_word["x0"]) - x_slope * (float(row["y"]) - reference_y)
            # A repeated numeric lane followed by a separate compact text box
            # is geometric evidence of adjacent value columns. Keep this scoped
            # to the detected right-side numeric lane so ordinary description
            # text such as sizes and model numbers cannot create new columns.
            if (
                numeric_x >= qty_x - 24.0
                and numeric_x < text_x
                and text_x - numeric_x <= 36.0
                and float(numeric_word["x1"]) <= float(text_word["x0"])
            ):
                lane_observations.extend([(numeric_x, row_index), (text_x, row_index)])
                adjacent_value_lanes.append((numeric_x, text_x, row_index))

    clusters: list[list[tuple[float, int]]] = []
    for observation in sorted(lane_observations):
        if clusters and abs(observation[0] - median(item[0] for item in clusters[-1])) <= 9.0:
            clusters[-1].append(observation)
        else:
            clusters.append([observation])
    minimum_lane_rows = max(2, int(len(rows_found) * 0.25 + 0.999))
    supported_lanes = sorted(
        (
            float(median(x for x, _ in cluster)),
            {row_index for _, row_index in cluster},
        )
        for cluster in clusters
        if len({row_index for _, row_index in cluster}) >= minimum_lane_rows
    )
    lane_starts = [value for value, _ in supported_lanes]
    lane_support_rows = [row_indexes for _, row_indexes in supported_lanes]
    # The repeated row anchors establish the outer ID and quantity lanes. Add
    # those medians explicitly in case OCR jitter split their observations.
    if not lane_starts or abs(lane_starts[0] - id_x) > 12.0:
        lane_starts.insert(0, id_x)
        lane_support_rows.insert(0, set())
    else:
        lane_starts[0] = id_x
    if abs(lane_starts[-1] - qty_x) > 22.0:
        lane_starts.append(qty_x)
        lane_support_rows.append(set())
    else:
        lane_starts[-1] = qty_x
    ordered_lanes = sorted(zip(lane_starts, lane_support_rows), key=lambda item: item[0])
    retained_lanes: list[tuple[float, set[int]]] = []
    for value, supporting_rows in ordered_lanes:
        if not retained_lanes or value - retained_lanes[-1][0] >= 12.0:
            retained_lanes.append((value, supporting_rows))
            continue
        previous = retained_lanes[-1][0]
        paired_rows = {
            row_index
            for left_x, right_x, row_index in adjacent_value_lanes
            if abs(left_x - previous) <= 9.0 and abs(right_x - value) <= 9.0
        }
        if len(paired_rows) >= minimum_lane_rows:
            retained_lanes.append((value, supporting_rows))
    lane_starts = [value for value, _ in retained_lanes]
    if len(lane_starts) < 3:
        return None, None
    lane_boundaries = [
        (left + right) / 2.0
        for left, right in zip(lane_starts, lane_starts[1:])
    ]

    # Discover labels from the closest source-text baseline above the first row.
    # Word boxes are assigned to the data lanes using positional midpoints; a
    # small left-column bias keeps split labels such as "Line No." together.
    first_y = float(rows_found[0]["y"])
    prior_lines = [
        line for line in lines
        if 1.0 <= first_y - float(line["y"]) <= 42.0
        and float(line.get("x1", 0)) >= qty_x - page_width * 0.035
        and float(line.get("x0", page_width)) <= id_x + page_width * 0.12
        and _is_probable_column_header_line(line)
        and not _is_repeated_identifier_data_line(line, lines, page_width)
    ]
    header_line = max(prior_lines, key=lambda item: float(item["y"])) if prior_lines else None
    headers = [""] * len(lane_starts)
    header_bbox: list[float] | None = None
    header_words_by_column: list[list[dict[str, Any]]] = [[] for _ in lane_starts]
    stacked_header_boxes_by_column: list[list[list[float]]] = [[] for _ in lane_starts]
    if header_line:
        bins: list[list[str]] = [[] for _ in lane_starts]
        header_words: list[dict[str, Any]] = []
        for run in header_line.get("runs", []):
            words = list(run.get("words", []))
            if words:
                header_words.extend(words)
            else:
                header_words.append(run)
        for word in sorted(header_words, key=lambda item: float(item.get("x0", 0))):
            text = _clean_cell(word.get("text", ""))
            if not text or _is_numeric_token(text):
                continue
            # Header labels are often centered within their cells. Using the
            # left edge can place a wide label into the preceding lane when it
            # straddles the geometric midpoint (for example, Amount near the
            # Price/Amount boundary). The word center is a better source anchor.
            x = (float(word.get("x0", 0)) + float(word.get("x1", 0))) / 2.0 - x_slope * (
                float(header_line["y"]) - reference_y
            )
            column = bisect_right(lane_boundaries, x)
            bins[column].append(text)
            header_words_by_column[column].append(word)
        headers = [re.sub(r"\s+", " ", " ".join(items)).strip(" _-:|") for items in bins]
        headers, stacked_header_boxes_by_column = _merge_adjacent_header_fragments(
            headers,
            header_words_by_column,
            header_line,
            lines,
            lane_boundaries,
            page_width,
        )
        header_bbox = [
            round(min(float(word.get("x0", 0)) for word in header_words), 2),
            round(min(float(word.get("y0", header_line["y"])) for word in header_words), 2),
            round(max(float(word.get("x1", 0)) for word in header_words), 2),
            round(max(float(word.get("y1", header_line["y"])) for word in header_words), 2),
        ] if header_words else None
        fragment_boxes = [box for column_boxes in stacked_header_boxes_by_column for box in column_boxes]
        if header_bbox and fragment_boxes:
            header_bbox = [
                round(min(header_bbox[0], *(box[0] for box in fragment_boxes)), 2),
                round(min(header_bbox[1], *(box[1] for box in fragment_boxes)), 2),
                round(max(header_bbox[2], *(box[2] for box in fragment_boxes)), 2),
                round(max(header_bbox[3], *(box[3] for box in fragment_boxes)), 2),
            ]

    if not any(headers):
        # Do not invent a fixed schema when the scan has no readable header.
        # The rows remain source-linked, and an empty header signals uncertainty.
        headers = [""] * len(lane_starts)

    row_values: list[list[str]] = []
    row_bboxes: list[list[float]] = []
    cell_bboxes: list[list[list[float] | None]] = []
    rows_need_review: list[bool] = []
    row_review_reasons: list[list[str]] = []
    for row in rows_found:
        spans = row["runs"]
        cells: list[list[str]] = [[] for _ in lane_starts]
        cell_spans: list[list[dict[str, Any]]] = [[] for _ in lane_starts]
        for run in row["runs"]:
            pieces = _split_run_at_column_boundaries(run, lane_boundaries)
            for piece, column in pieces:
                if column is None:
                    x = float(piece["x0"]) - x_slope * (float(row["y"]) - reference_y)
                    column = bisect_right(lane_boundaries, x)
                cells[column].append(_clean_cell(piece.get("text", "")))
                cell_spans[column].append(piece)
        row_values.append([
            re.sub(r"\s+", " ", " ".join(cell)).strip()
            for cell in cells
        ])
        cell_bboxes.append([
            [
                round(min(float(run["x0"]) for run in cell_runs), 2),
                round(min(float(run["y0"]) for run in cell_runs), 2),
                round(max(float(run["x1"]) for run in cell_runs), 2),
                round(max(float(run["y1"]) for run in cell_runs), 2),
            ] if cell_runs else None
            for cell_runs in cell_spans
        ])
        row_bboxes.append([
            round(min(float(run["x0"]) for run in spans), 2),
            round(min(float(run["y0"]) for run in spans), 2),
            round(max(float(run["x1"]) for run in spans), 2),
            round(max(float(run["y1"]) for run in spans), 2),
        ])
        is_aligned = id(row) in aligned_row_ids
        rows_need_review.append(not is_aligned)
        row_review_reasons.append([] if is_aligned else ["row_alignment_uncertain"])

    source_bbox = [
        round(min(id_x, min(float(row["x0"]) for row in prior_lines) if prior_lines else id_x), 2),
        round(header_bbox[1] if header_bbox else row_bboxes[0][1], 2),
        round(max(float(row["quantity"]["x1"]) for row in rows_found), 2),
        round(max(bbox[3] for bbox in row_bboxes), 2),
    ]
    header_distance = first_y - float(header_line["y"]) if header_line else None
    payload = {
        "table_index": 1,
        "table_role": "line_items",
        "layout_type": "item_table",
        "is_source_table": True,
        "headers": headers,
        "header_rows": [headers] if any(headers) else [],
        "header_cells": [
            {
                "row": 0,
                "column": index,
                "text": label,
                "rowspan": 1,
                "colspan": 1,
                "source_bbox": _bbox_union([
                    [
                        min(float(word["x0"]) for word in words),
                        min(float(word["y0"]) for word in words),
                        max(float(word["x1"]) for word in words),
                        max(float(word["y1"]) for word in words),
                    ] for words in [header_words_by_column[index]] if words
                ] + stacked_header_boxes_by_column[index]),
            }
            for index, label in enumerate(headers) if label
        ],
        "header_detected": bool(any(headers)),
        "header_source": "detected_from_repeated_row_geometry" if any(headers) else "not_detected",
        "column_count": len(lane_starts),
        "row_count": len(row_values),
        "rows": row_values,
        "source_bbox": source_bbox,
        "header_source_bbox": header_bbox,
        "header_source_bboxes": [header_bbox] if header_bbox else [],
        "row_source_bboxes": row_bboxes,
        "cell_source_bboxes": cell_bboxes,
        "rows_need_review": rows_need_review,
        "row_review_reasons": row_review_reasons,
        "detection_strategy": "ocr_repeated_row_geometry",
        "summary_items": [],
        "metadata_items": [],
        "detection_confidence": round(min(0.91, 0.58 + len(row_values) * 0.035 + (0.08 if any(headers) else 0.0)), 2),
        "_header_distance": header_distance,
    }
    return payload, float(source_bbox[3])


def _extract_ocr_rows_by_vertical_item_anchors(page: Any) -> dict[str, Any] | None:
    """Rebuild OCR item rows using vertically centered item-number anchors.

    On compact scanned tables, multiple wrapped description lines can share a
    visual baseline with text from the neighbouring row. This routine anchors
    each row to the numeric identifier in the left lane, uses the midpoints
    between adjacent identifiers as cell boundaries, then assigns each OCR
    word to the closest row band. It is only used for OCR-backed pages and is
    deliberately conservative: callers keep the current table if this returns
    too few rows or too little description text.
    """
    if not getattr(page, "ocr_word_count", 0):
        return None

    try:
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        raw_words = page.get_text("words", sort=True)
    except Exception:
        logger.exception("Unable to read OCR word anchors")
        return None

    if page_width <= 0 or page_height <= 0:
        return None

    words: list[dict[str, Any]] = []
    for raw in raw_words:
        if len(raw) < 5:
            continue
        text = _clean_cell(raw[4])
        if not text:
            continue
        try:
            x0, y0, x1, y1 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        except (TypeError, ValueError, OverflowError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        words.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
            "text": text,
        })

    if len(words) < 12:
        return None

    # Numeric row identifiers in the left-side lane. The broad relative bounds
    # let page margins vary while avoiding digits inside the main description.
    id_pattern = re.compile(r"^(\d{1,3})[.)]?$")
    id_candidates: list[dict[str, Any]] = []
    for word in words:
        relative_x = word["cx"] / page_width
        relative_y = word["cy"] / page_height
        if not (0.10 <= relative_x <= 0.34 and 0.18 <= relative_y <= 0.91):
            continue
        match = id_pattern.fullmatch(word["text"].strip())
        if not match:
            continue
        copied = dict(word)
        copied["identifier"] = str(int(match.group(1)))
        id_candidates.append(copied)

    id_candidates.sort(key=lambda item: (item["cy"], item["x0"]))
    # Duplicate OCR boxes can occur around a printed digit. Collapse only boxes
    # at virtually the same vertical position, not separate repeated item IDs.
    identifiers: list[dict[str, Any]] = []
    for candidate in id_candidates:
        duplicate_index = next((
            i for i, previous in enumerate(identifiers)
            if abs(candidate["cy"] - previous["cy"]) <= max(3.0, page_height * 0.004)
            and abs(candidate["cx"] - previous["cx"]) <= page_width * 0.035
        ), None)
        if duplicate_index is None:
            identifiers.append(candidate)
        elif candidate["x1"] - candidate["x0"] > identifiers[duplicate_index]["x1"] - identifiers[duplicate_index]["x0"]:
            identifiers[duplicate_index] = candidate

    if len(identifiers) < 4:
        return None

    # Retain the largest cluster of vertically spaced identifiers. This drops
    # isolated digits in document metadata above or below the item table.
    max_gap = max(24.0, min(38.0, page_height * 0.045))
    clusters: list[list[dict[str, Any]]] = []
    for candidate in identifiers:
        if not clusters or candidate["cy"] - clusters[-1][-1]["cy"] > max_gap:
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)
    cluster = max(clusters, key=lambda group: (len(group), group[-1]["cy"] - group[0]["cy"]))
    if len(cluster) < 4:
        return None

    # Build row-cell vertical boundaries halfway between neighbouring item
    # numbers. This follows the scanned table's row rhythm, including wrapped
    # descriptions, instead of requiring a whole row to fit on one OCR baseline.
    centers = [float(item["cy"]) for item in cluster]
    if any(right <= left for left, right in zip(centers, centers[1:])):
        return None
    boundaries = [centers[0] - (centers[1] - centers[0]) / 2.0]
    boundaries.extend((left + right) / 2.0 for left, right in zip(centers, centers[1:]))
    boundaries.append(centers[-1] + (centers[-1] - centers[-2]) / 2.0)

    def is_qty_token(text: str) -> bool:
        clean = _clean_cell(text).strip()
        return bool(re.fullmatch(r"(?:\(?-?\d[\d,.]*\)?|[-–—])", clean))

    quantity_candidates = [
        word for word in words
        if word["x0"] >= page_width * 0.50
        and boundaries[0] <= word["cy"] <= boundaries[-1]
        and is_qty_token(word["text"])
    ]
    if len(quantity_candidates) < max(3, int(len(cluster) * 0.45)):
        return None

    # The rightmost repeated numeric lane is the quantity column for this
    # three-column OCR fallback. Only apply this helper when the detected
    # source header confirms a quantity-like final column (checked by caller).
    quantity_x = float(median(word["x0"] for word in quantity_candidates))
    identifier_right = float(median(item["x1"] for item in cluster))
    description_x_min = identifier_right + max(1.0, page_width * 0.004)
    description_x_max = quantity_x - max(2.0, page_width * 0.007)
    if description_x_max <= description_x_min:
        return None

    row_words: list[list[dict[str, Any]]] = [[] for _ in cluster]
    for word in words:
        if not (description_x_min <= word["x0"] < description_x_max):
            continue
        if not (boundaries[0] <= word["cy"] <= boundaries[-1]):
            continue
        row_index = bisect_right(boundaries, word["cy"]) - 1
        row_index = max(0, min(row_index, len(cluster) - 1))
        row_words[row_index].append(word)

    row_values: list[list[str]] = []
    row_bboxes: list[list[float]] = []
    for index, identifier in enumerate(cluster):
        # Sort by visual line, then left-to-right within each line.
        description_words = sorted(row_words[index], key=lambda item: (round(item["cy"], 1), item["x0"]))
        deduped: list[dict[str, Any]] = []
        for word in description_words:
            duplicate = any(
                word["text"].casefold() == previous["text"].casefold()
                and abs(word["cy"] - previous["cy"]) <= 2.0
                and abs(word["x0"] - previous["x0"]) <= 2.5
                for previous in deduped[-12:]
            )
            if not duplicate:
                deduped.append(word)
        description = re.sub(r"\s+", " ", " ".join(item["text"] for item in deduped)).strip()

        row_quantities = [
            word for word in quantity_candidates
            if boundaries[index] <= word["cy"] <= boundaries[index + 1]
        ]
        quantity = min(row_quantities, key=lambda item: abs(item["cy"] - centers[index])) if row_quantities else None
        quantity_text = _clean_cell(quantity["text"]) if quantity else ""
        row_values.append([identifier["identifier"], description, quantity_text])

        row_boxes = [identifier, *deduped]
        if quantity:
            row_boxes.append(quantity)
        row_bboxes.append([
            round(min(item["x0"] for item in row_boxes), 2),
            round(min(item["y0"] for item in row_boxes), 2),
            round(max(item["x1"] for item in row_boxes), 2),
            round(max(item["y1"] for item in row_boxes), 2),
        ])

    described_rows = sum(bool(row[1]) for row in row_values)
    if described_rows < max(3, int(len(row_values) * 0.70)):
        return None

    return {
        "rows": row_values,
        "row_bboxes": row_bboxes,
        "row_count": len(row_values),
        "source_bbox": [
            round(min(box[0] for box in row_bboxes), 2),
            round(min(box[1] for box in row_bboxes), 2),
            round(max(box[2] for box in row_bboxes), 2),
            round(max(box[3] for box in row_bboxes), 2),
        ],
        "recovery_strategy": "ocr_vertical_item_anchor_bands",
        "detection_confidence": round(min(0.90, 0.60 + len(row_values) * 0.012), 2),
    }


def _extract_ocr_rows_by_dynamic_identifier_anchors(
    page: Any,
    visual_lines: list[dict[str, Any]],
    current_headers: list[str] | None = None,
) -> dict[str, Any] | None:
    """Rebuild scan rows from a detected header line and a repeated ID lane.

    Unlike the legacy three-column recovery, this preserves every observed
    header lane. Row bands come from printed identifiers; text and values are
    assigned using their visual runs and the source header geometry.
    """
    try:
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        raw_words = page.get_text("words", sort=True)
    except Exception:
        logger.exception("Unable to read OCR words for dynamic identifier recovery")
        return None
    if page_width <= 0 or page_height <= 0:
        return None

    words: list[dict[str, Any]] = []
    for raw in raw_words:
        if len(raw) < 5:
            continue
        text = _clean_cell(raw[4]).strip()
        try:
            x0, y0, x1, y1 = (float(raw[i]) for i in range(4))
        except (TypeError, ValueError, OverflowError):
            continue
        if text and x1 > x0 and y1 > y0:
            words.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "cy": (y0 + y1) / 2.0})
    if len(words) < 12:
        return None

    # Select the strongest numeric lane near the left side of the page, then
    # the largest vertically contiguous group in that lane. This is evidence
    # from the page, not a configured item-number column coordinate.
    numeric_id = re.compile(r"^(\d+)[.)]?$|^(\d+)$")
    candidates = [
        word for word in words
        if 0.015 <= word["x0"] / page_width <= 0.18
        and 0.12 <= word["cy"] / page_height <= 0.94
        and numeric_id.fullmatch(word["text"])
    ]
    if len(candidates) < 4:
        return None
    x_groups: list[list[dict[str, Any]]] = []
    for word in sorted(candidates, key=lambda item: item["x0"]):
        group = next((g for g in x_groups if abs(float(median(i["x0"] for i in g)) - word["x0"]) <= max(3.0, page_width * 0.015)), None)
        if group is None:
            x_groups.append([word])
        else:
            group.append(word)
    id_lane = max(x_groups, key=len)
    id_lane.sort(key=lambda item: item["cy"])
    y_clusters: list[list[dict[str, Any]]] = []
    row_gap_limit = max(26.0, min(58.0, page_height * 0.07))
    for word in id_lane:
        if not y_clusters or word["cy"] - y_clusters[-1][-1]["cy"] > row_gap_limit:
            y_clusters.append([word])
        else:
            y_clusters[-1].append(word)
    anchors = max(y_clusters, key=lambda group: (len(group), group[-1]["cy"] - group[0]["cy"]))
    deduped_anchors: list[dict[str, Any]] = []
    for word in anchors:
        if deduped_anchors and abs(word["cy"] - deduped_anchors[-1]["cy"]) <= max(3.0, page_height * 0.004):
            continue
        deduped_anchors.append(word)
    anchors = deduped_anchors
    if len(anchors) < 4:
        return None
    identifier_values = [
        int(re.fullmatch(r"(\d+)[.)]?", _clean_cell(item["text"])).group(1))
        for item in anchors
    ]
    identifier_steps = [right - left for left, right in zip(identifier_values, identifier_values[1:])]
    if (
        any(step <= 0 for step in identifier_steps)
        or sum(step in (1, 2) for step in identifier_steps) < max(3, int(len(identifier_steps) * 0.80))
    ):
        return None
    identifier_span = identifier_values[-1] - identifier_values[0] + 1
    if not (len(anchors) * 0.75 <= identifier_span <= len(anchors) * 1.5):
        return None

    first_y, last_y = anchors[0]["cy"], anchors[-1]["cy"]
    eligible_header_lines: list[dict[str, Any]] = []
    for line in visual_lines:
        distance = first_y - float(line["y"])
        if not (2.0 <= distance <= max(90.0, page_height * 0.16)):
            continue
        labels = [
            run for run in line.get("runs", [])
            if re.search(r"[A-Za-zÀ-ž]", _clean_cell(run.get("text", "")))
            and len(_clean_cell(run.get("text", ""))) <= 42
        ]
        if labels and len(labels) <= 16:
            eligible_header_lines.append({"line": line, "labels": labels, "distance": distance})
    header_candidates: list[tuple[float, list[dict[str, Any]]]] = []
    for entry in eligible_header_lines:
        nearby = [
            other for other in eligible_header_lines
            if abs(float(other["line"]["y"]) - float(entry["line"]["y"])) <= max(5.0, page_height * 0.01)
        ]
        combined: list[dict[str, Any]] = []
        for other in nearby:
            combined.extend(other["labels"])
        combined.sort(key=lambda run: float(run["x0"]))
        # A spaced or stacked header can split into separate OCR baselines;
        # merge only nearby runs above the first item anchor.
        deduped_runs: list[dict[str, Any]] = []
        for run in combined:
            if deduped_runs and abs(float(run["x0"]) - float(deduped_runs[-1]["x0"])) <= 2.5 and abs(float(run["y0"]) - float(deduped_runs[-1]["y0"])) <= max(5.0, page_height * 0.01):
                continue
            parts = run.get("words", [])
            header_tokens = _clean_cell(run.get("text", "")).split()
            current_header_tokens = {
                re.sub(r"[^a-z0-9]", "", _clean_cell(value).casefold())
                for value in (current_headers or []) if _clean_cell(value)
            }
            split_as_observed_columns = (
                len(header_tokens) >= 2
                and all(re.sub(r"[^a-z0-9]", "", token.casefold()) in current_header_tokens for token in header_tokens)
            )
            if split_as_observed_columns and len(parts) <= 1:
                # The OCR engine can return two adjacent, separately detected
                # header labels as one word box. Split the bbox proportionally
                # to the observed tokens; only do so when the existing
                # extraction independently detected each label as a column.
                x0, x1 = float(run["x0"]), float(run["x1"])
                total_chars = sum(len(token) for token in header_tokens)
                gap = max(1.0, (x1 - x0) * 0.15)
                usable = max(1.0, (x1 - x0) - gap * (len(header_tokens) - 1))
                cursor = x0
                parts = []
                for token in header_tokens:
                    width = usable * len(token) / max(1, total_chars)
                    parts.append({"text": token, "x0": cursor, "y0": float(run["y0"]), "x1": cursor + width, "y1": float(run["y1"])})
                    cursor += width + gap
            if split_as_observed_columns and len(parts) >= 2:
                deduped_runs.extend({
                    "text": _clean_cell(part["text"]), "x0": float(part["x0"]), "y0": float(part["y0"]),
                    "x1": float(part["x1"]), "y1": float(part["y1"]), "words": [part],
                } for part in parts)
            elif len(parts) > 1 and all(re.search(r"[A-Za-zÀ-ž]", _clean_cell(part.get("text", ""))) and len(_clean_cell(part.get("text", ""))) <= 24 for part in parts):
                deduped_runs.extend({
                    "text": _clean_cell(part["text"]), "x0": float(part["x0"]), "y0": float(part["y0"]),
                    "x1": float(part["x1"]), "y1": float(part["y1"]), "words": [part],
                } for part in parts)
            else:
                deduped_runs.append(run)
        if len(deduped_runs) < 3 or len(deduped_runs) > 16:
            continue
        score = len(deduped_runs) * 4.0 - float(entry["distance"]) * 0.025
        header_candidates.append((score, deduped_runs))
    if not header_candidates:
        return None
    _, header_runs = max(header_candidates, key=lambda item: item[0])
    header_runs = sorted(header_runs, key=lambda run: float(run["x0"]))
    headers = [re.sub(r"\s+", " ", _clean_cell(run["text"])).strip() for run in header_runs]
    lane_starts = [float(run["x0"]) for run in header_runs]
    if len(set(round(value, 1) for value in lane_starts)) != len(lane_starts):
        return None

    centers = [float(item["cy"]) for item in anchors]
    if any(right <= left for left, right in zip(centers, centers[1:])):
        return None
    anchor_heights = [max(1.0, float(item["y1"]) - float(item["y0"])) for item in anchors]
    anchor_line_y = [
        min((float(line["y"]) for line in visual_lines), key=lambda y: abs(y - center))
        for center in centers
    ]
    # Text between two identifier baselines is a continuation of the earlier
    # row. Use actual OCR baselines (rather than word-box centers) so the next
    # item's number and its description stay together.
    row_boundaries = [anchor_line_y[0] - max(2.0, anchor_heights[0] * 0.45)]
    row_boundaries.extend(anchor_line_y[1:])
    typical_gap = float(median([right - left for left, right in zip(anchor_line_y, anchor_line_y[1:])]))
    row_boundaries.append(anchor_line_y[-1] + max(anchor_heights[-1], typical_gap * 1.15))

    rows: list[list[str]] = []
    row_bboxes: list[list[float]] = []
    for row_index, anchor in enumerate(anchors):
        cells: list[list[str]] = [[] for _ in headers]
        used: list[dict[str, Any]] = []
        for line in visual_lines:
            for run in line.get("runs", []):
                run_words = [word for word in run.get("words", []) if word.get("text", "").strip()]
                if not run_words:
                    continue
                line_y = float(line["y"])
                text_column = min(range(len(lane_starts)), key=lambda col: abs(float(run["x0"]) - lane_starts[col]))
                if text_column <= 1:
                    if not (row_boundaries[row_index] <= line_y < row_boundaries[row_index + 1]):
                        continue
                else:
                    # Compact numeric/code/unit cells may be on a baseline
                    # slightly above or below the identifier/description.
                    # Assign those by nearest observed item baseline while
                    # keeping wrapped prose within the item-to-item band.
                    nearest_row = min(range(len(anchor_line_y)), key=lambda item: abs(line_y - anchor_line_y[item]))
                    if nearest_row != row_index or abs(line_y - anchor_line_y[row_index]) > max(10.0, typical_gap * 0.65):
                        continue
                combined_text = _clean_cell(run.get("text", "")).strip()
                quantity_column = next((
                    column for column, label in enumerate(headers)
                    if re.search(r"\b(?:qty|quantity)\b", re.sub(r"[^a-z]", "", label.casefold()))
                ), None)
                unit_column = next((
                    column for column, label in enumerate(headers)
                    if re.search(r"\b(?:unit|uom)\b", label, flags=re.IGNORECASE)
                ), None)
                combined_measure = re.fullmatch(r"([+-]?\d[\d,.]*)\s+([A-Za-zÀ-ž][A-Za-zÀ-ž0-9/.-]*)", combined_text)
                if (
                    combined_measure and quantity_column is not None and unit_column == quantity_column + 1
                    and float(run["x0"]) < lane_starts[unit_column] <= float(run["x1"]) + 1.0
                ):
                    cells[quantity_column].append(combined_measure.group(1))
                    cells[unit_column].append(combined_measure.group(2))
                    used.extend(run_words)
                    continue
                if len(run_words) == 1:
                    spaced = _clean_cell(run_words[0].get("text", "")).split()
                    if len(spaced) == 2 and _is_numeric_token(spaced[0]) and re.search(r"[A-Za-zÀ-ž]", spaced[1]):
                        source_word = run_words[0]
                        x0, x1 = float(source_word["x0"]), float(source_word["x1"])
                        geometrically_split = next((
                            column_index for column_index in range(len(lane_starts) - 1)
                            if lane_starts[column_index] <= x0 < lane_starts[column_index + 1] <= x1 + 1.0
                        ), None)
                        if geometrically_split is not None:
                            cells[geometrically_split].append(spaced[0])
                            cells[geometrically_split + 1].append(spaced[1])
                            used.append(source_word)
                            continue
                        gap = max(1.0, (x1 - x0) * 0.15)
                        part_width = max(1.0, (x1 - x0 - gap) / 2.0)
                        run_words = [
                            {"text": spaced[0], "x0": x0, "y0": float(source_word["y0"]), "x1": x0 + part_width, "y1": float(source_word["y1"])},
                            {"text": spaced[1], "x0": x0 + part_width + gap, "y0": float(source_word["y0"]), "x1": x1, "y1": float(source_word["y1"])},
                        ]
                column = text_column
                # A run can contain adjacent right-hand cells. Split it at
                # observed header lane starts only when its first token is
                # numeric and later token positions support separate lanes.
                for word in run_words:
                    word_cy = (float(word["y0"]) + float(word["y1"])) / 2.0
                    if abs(word_cy - float(anchor["cy"])) <= max(3.5, page_height * 0.004) and abs(float(word["x0"]) - float(anchor["x0"])) <= page_width * 0.025:
                        continue
                    word_column = column
                    nearest_column = min(range(len(lane_starts)), key=lambda col: abs(float(word["x0"]) - lane_starts[col]))
                    # Numeric values have a stronger column anchor than prose.
                    # Do not split ordinary descriptions merely because a
                    # trailing word happens to extend into a numeric lane.
                    if (
                        _is_numeric_token(str(word["text"]))
                        and nearest_column >= 2
                        and abs(float(word["x0"]) - lane_starts[nearest_column]) <= max(10.0, page_width * 0.025)
                    ):
                        word_column = nearest_column
                    cells[word_column].append(_clean_cell(word["text"]))
                    used.append(word)
        # The row identifier is taken from its own detected source token.
        identifier = _clean_cell(anchor["text"])
        cells[0].insert(0, identifier)
        row = [re.sub(r"\s+", " ", " ".join(cell)).strip() for cell in cells]
        rows.append(row)
        row_words = [anchor, *used]
        row_bboxes.append([
            round(min(float(word["x0"]) for word in row_words), 2),
            round(min(float(word["y0"]) for word in row_words), 2),
            round(max(float(word["x1"]) for word in row_words), 2),
            round(max(float(word["y1"]) for word in row_words), 2),
        ])

    described = sum(any(re.search(r"[A-Za-zÀ-ž]", value) for value in row[1:]) for row in rows)
    if described < max(3, int(len(rows) * 0.7)):
        return None
    header_bbox = [
        round(min(float(run["x0"]) for run in header_runs), 2),
        round(min(float(run["y0"]) for run in header_runs), 2),
        round(max(float(run["x1"]) for run in header_runs), 2),
        round(max(float(run["y1"]) for run in header_runs), 2),
    ]
    return {
        "headers": headers,
        "header_rows": [headers],
        "header_cells": [
            {"row": 0, "column": index, "text": label, "rowspan": 1, "colspan": 1,
             "source_bbox": [round(float(v), 2) for v in (run["x0"], run["y0"], run["x1"], run["y1"])]}
            for index, (label, run) in enumerate(zip(headers, header_runs))
        ],
        "rows": rows,
        "row_bboxes": row_bboxes,
        "row_count": len(rows),
        "source_bbox": [
            round(min(header_bbox[0], *(box[0] for box in row_bboxes)), 2),
            round(min(header_bbox[1], *(box[1] for box in row_bboxes)), 2),
            round(max(header_bbox[2], *(box[2] for box in row_bboxes)), 2),
            round(max(header_bbox[3], *(box[3] for box in row_bboxes)), 2),
        ],
        "header_source_bbox": header_bbox,
        "detection_confidence": 0.84,
        "row_recovery_strategy": "ocr_dynamic_identifier_header_lanes",
        "rows_need_review": [not any(re.search(r"[A-Za-zÀ-ž]", value) for value in row[1:]) for row in rows],
    }


def _attach_pre_row_context(page: Any, table: dict[str, Any], visual_lines: list[dict[str, Any]]) -> None:
    """Keep source text between a table header and its first item as context."""
    if table.get("context_rows"):
        return
    header_box = table.get("header_source_bbox")
    row_boxes = table.get("row_source_bboxes") or []
    first_row = next((box for box in row_boxes if isinstance(box, (list, tuple)) and len(box) == 4), None)
    table_box = table.get("source_bbox")
    if not (isinstance(header_box, (list, tuple)) and len(header_box) == 4 and first_row and table_box and len(table_box) == 4):
        return
    y0, y1 = float(header_box[3]), float(first_row[1])
    if not 0 < y1 - y0 <= 90:
        return
    contexts = []
    for line in visual_lines:
        if not y0 < float(line.get("y", 0)) < y1:
            continue
        runs = [run for run in line.get("runs", []) if float(run.get("x1", 0)) >= float(table_box[0]) and float(run.get("x0", 0)) <= float(table_box[2])]
        text = " ".join(str(run.get("text", "")).strip() for run in runs).strip()
        if text and re.search(r"[A-Za-zÀ-ž]", text) and not _is_item_identifier(text):
            box = [min(float(run["x0"]) for run in runs), min(float(run["y0"]) for run in runs), max(float(run["x1"]) for run in runs), max(float(run["y1"]) for run in runs)]
            contexts.append({"type": "section_context", "text": text, "source_bbox": [round(value, 2) for value in box]})
    if contexts:
        table["context_rows"] = contexts


def _attach_parent_item_context(page: Any, table: dict[str, Any], visual_lines: list[dict[str, Any]]) -> None:
    """Attach a source-visible parent label to lettered child rows when aligned."""
    rows = table.get("rows") or []
    child_ids = [
        _clean_cell(row[0]).strip().casefold()
        for row in rows if row and re.fullmatch(r"[a-z]", _clean_cell(row[0]).strip(), flags=re.IGNORECASE)
    ]
    if len(child_ids) < 2:
        return
    first_row_bbox = next((box for box in table.get("row_source_bboxes", []) if box and len(box) == 4), None)
    if not first_row_bbox:
        return
    try:
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        raw_words = page.get_text("words", sort=True)
    except Exception:
        return
    child_words = []
    for row_index, row in enumerate(rows):
        if not row or _clean_cell(row[0]).strip().casefold() not in child_ids:
            continue
        row_bbox = table.get("row_source_bboxes", [])[row_index] if row_index < len(table.get("row_source_bboxes", [])) else None
        if not row_bbox:
            continue
        row_center = (float(row_bbox[1]) + float(row_bbox[3])) / 2.0
        for raw in raw_words:
            if len(raw) < 5 or _clean_cell(raw[4]).strip().casefold() != _clean_cell(row[0]).strip().casefold():
                continue
            x0, y0, x1, y1 = (float(raw[i]) for i in range(4))
            cy = (y0 + y1) / 2.0
            if abs(cy - row_center) <= max(4.0, (float(row_bbox[3]) - float(row_bbox[1])) * 0.65):
                child_words.append({"x0": x0, "x1": x1, "cy": cy, "text": _clean_cell(raw[4]).strip()})
    if not child_words:
        return
    child_lane_x = float(median(word["x0"] for word in child_words))
    first_child_y = min(word["cy"] for word in child_words)
    candidates: list[tuple[float, dict[str, Any], str]] = []
    for line in visual_lines:
        distance = first_child_y - float(line["y"])
        if not (2.0 < distance <= min(page_height * 0.22, 190.0)):
            continue
        for run in line.get("runs", []):
            text = _clean_cell(run.get("text", "")).strip()
            if not re.fullmatch(r"\d+[.)]?", text) or abs(float(run["x0"]) - child_lane_x) > page_width * 0.025:
                continue
            descriptions = [
                other for other in line.get("runs", [])
                if float(other["x0"]) > float(run["x1"])
                and re.search(r"[A-Za-zÀ-ž]", _clean_cell(other.get("text", "")))
            ]
            if not descriptions:
                continue
            description = min(descriptions, key=lambda other: float(other["x0"]))
            candidates.append((distance, run, _clean_cell(description["text"]).strip()))
    parent_identifier: str | None = None
    parent_description_run: dict[str, Any] | None = None
    parent_id_run: dict[str, Any] | None = None
    if candidates:
        _, parent_id_run, description = min(candidates, key=lambda item: item[0])
        # The paired text run is on the same visual line and begins to the
        # right of the source identifier.
        matching_line = min(visual_lines, key=lambda line: abs(float(line["y"]) - ((float(parent_id_run["y0"]) + float(parent_id_run["y1"])) / 2.0)))
        parent_description_run = next((
            run for run in matching_line.get("runs", [])
            if _clean_cell(run.get("text", "")).strip() == description
            and float(run["x0"]) > float(parent_id_run["x1"])
        ), None)
        parent_identifier = _clean_cell(parent_id_run["text"]).strip().rstrip(".)")
    else:
        # If OCR missed the parent number, preserve a preceding source label
        # only when it forms a distinct heading above multiple aligned
        # label/value lines that lead into the lettered child rows. The ID stays
        # null and reviewable; the parser does not synthesize a number.
        description_cell_boxes = table.get("cell_source_bboxes") or []
        description_lane_x = None
        if description_cell_boxes and description_cell_boxes[0] and len(description_cell_boxes[0]) > 2:
            description_box = description_cell_boxes[0][2]
            if description_box:
                description_lane_x = float(description_box[0])
        if description_lane_x is None:
            description_lane_x = min((float(raw[0]) for raw in raw_words if len(raw) >= 5 and _clean_cell(raw[4]).strip().casefold() == "main"), default=child_lane_x + page_width * 0.08)
        label_candidates: list[tuple[float, dict[str, Any]]] = []
        for line in visual_lines:
            distance = first_child_y - float(line["y"])
            if not (2.0 < distance <= min(page_height * 0.22, 190.0)):
                continue
            for run in line.get("runs", []):
                text = _clean_cell(run.get("text", "")).strip()
                if (
                    len(text.split()) < 3 or len(text) > 100 or ":" in text
                    or not re.search(r"[A-Za-zÀ-ž]", text)
                    or abs(float(run["x0"]) - description_lane_x) > page_width * 0.035
                ):
                    continue
                aligned_detail_lines = [
                    other for other_line in visual_lines
                    if float(line["y"]) < float(other_line["y"]) < first_child_y
                    for other in other_line.get("runs", [])
                    if ":" in _clean_cell(other.get("text", ""))
                    and abs(float(other["x0"]) - description_lane_x) <= page_width * 0.035
                ]
                if len(aligned_detail_lines) >= 2:
                    label_candidates.append((distance, run))
        if not label_candidates:
            return
        _, parent_description_run = min(label_candidates, key=lambda item: item[0])
        description = _clean_cell(parent_description_run.get("text", "")).strip()

    if parent_description_run is None and parent_id_run is None:
        return
    source_boxes = [run for run in (parent_id_run, parent_description_run) if run]
    parent_bbox = [
        round(min(float(run["x0"]) for run in source_boxes), 2),
        round(min(float(run["y0"]) for run in source_boxes), 2),
        round(max(float(run["x1"]) for run in source_boxes), 2),
        round(max(float(run["y1"]) for run in source_boxes), 2),
    ]
    table["parent_item_context"] = {
        "identifier": parent_identifier,
        "description": description,
        "source_bbox": parent_bbox,
        "child_item_ids": child_ids,
        "source_page": int(getattr(page, "number", 0)) + 1,
        "identifier_needs_review": parent_identifier is None,
    }
    table["row_parent_links"] = [
        {"row_index": index, "child_identifier": _clean_cell(row[0]).strip(), "parent_identifier": parent_identifier}
        for index, row in enumerate(rows)
        if row and _clean_cell(row[0]).strip().casefold() in child_ids
    ]
    if parent_identifier is None:
        table["review_required"] = True
        table["validation_status"] = "needs_review"
        table.setdefault("review_issues", []).append({
            "code": "parent_identifier_not_read",
            "message": "Konteks induk terlihat, tetapi nomor induknya tidak terbaca OCR dan perlu diperiksa.",
            "source_bbox": parent_bbox,
        })


def _extract_ocr_rows_by_repeated_numeric_lanes(
    page: Any,
    visual_lines: list[dict[str, Any]],
    *,
    after_y: float = 0.0,
) -> dict[str, Any] | None:
    """Recover headerless OCR detail rows from repeated description/value lanes.

    This is a last-resort geometry path for pages where OCR sees the cells but
    neither item identifiers nor a usable header. It requires multiple nearby
    description lines and at least three numeric lanes repeated at the same x
    positions, which avoids treating isolated totals or metadata as item rows.
    """
    candidates: list[dict[str, Any]] = []
    for line in visual_lines:
        y = float(line.get("y", 0.0))
        if y <= after_y:
            continue
        words = [word for run in line.get("runs", []) for word in run.get("words", [])]
        numeric_words = [word for word in words if _is_numeric_token(str(word.get("text", "")))]
        if len(numeric_words) < 3:
            continue
        numeric_left = min(float(word["x0"]) for word in numeric_words)
        descriptions = [
            run for run in line.get("runs", [])
            if float(run.get("x0", 0.0)) < numeric_left
            and len(re.findall(r"[A-Za-zÀ-ž]+", str(run.get("text", "")))) >= 2
            and not _is_numeric_token(str(run.get("text", "")))
        ]
        if not descriptions:
            continue
        description = min(descriptions, key=lambda run: float(run["x0"]))
        candidates.append({
            "line": line,
            "description": description,
            "numeric_words": numeric_words,
            "y": y,
        })

    if len(candidates) < 2:
        return None

    groups: list[list[dict[str, Any]]] = []
    for candidate in candidates:
        if groups:
            previous = groups[-1][-1]
            vertical_gap = candidate["y"] - previous["y"]
            same_description_lane = abs(
                float(candidate["description"]["x0"])
                - float(previous["description"]["x0"])
            ) <= 14.0
            if 0 < vertical_gap <= 34.0 and same_description_lane:
                groups[-1].append(candidate)
                continue
        groups.append([candidate])

    valid_groups: list[tuple[int, list[dict[str, Any]], list[float]]] = []
    for group in groups:
        if len(group) < 2:
            continue
        clusters: list[list[float]] = []
        for candidate in group:
            for word in sorted(candidate["numeric_words"], key=lambda item: float(item["x0"])):
                x = float(word["x0"])
                nearest = min(
                    range(len(clusters)),
                    key=lambda index: abs(median(clusters[index]) - x),
                    default=None,
                )
                if nearest is not None and abs(median(clusters[nearest]) - x) <= 12.0:
                    clusters[nearest].append(x)
                else:
                    clusters.append([x])
        lane_clusters = [
            cluster for cluster in clusters
            if len({
                index for index, candidate in enumerate(group)
                if any(abs(float(word["x0"]) - median(cluster)) <= 12.0
                       for word in candidate["numeric_words"])
            }) == len(group)
        ]
        lanes = sorted(float(median(cluster)) for cluster in lane_clusters)
        if len(lanes) < 3 or any(right - left < 18.0 for left, right in zip(lanes, lanes[1:])):
            continue
        if any(
            sum(
                min(abs(float(word["x0"]) - lane) for lane in lanes) <= 14.0
                for word in candidate["numeric_words"]
            ) < 3
            for candidate in group
        ):
            continue
        valid_groups.append((len(group), group, lanes))

    if not valid_groups:
        return None
    _count, group, lanes = max(valid_groups, key=lambda item: (item[0], len(item[2])))

    def lane_for(word: dict[str, Any]) -> int | None:
        index = min(range(len(lanes)), key=lambda value: abs(float(word["x0"]) - lanes[value]))
        return index if abs(float(word["x0"]) - lanes[index]) <= 14.0 else None

    boundaries = [(left + right) / 2.0 for left, right in zip(lanes, lanes[1:])]
    rows: list[list[str]] = []
    row_boxes: list[list[float]] = []
    row_words: list[list[dict[str, Any]]] = []
    for candidate in group:
        description = candidate["description"]
        values = [str(description.get("text", "")).strip()] + [""] * len(lanes)
        included = list(description.get("words", []))
        lane_words: list[list[dict[str, Any]]] = [[] for _ in lanes]
        for word in candidate["numeric_words"]:
            index = lane_for(word)
            if index is not None:
                lane_words[index].append(word)
        for index, words in enumerate(lane_words):
            if not words:
                continue
            numeric_text = " ".join(str(word["text"]).strip() for word in sorted(words, key=lambda item: float(item["x0"])))
            values[index + 1] = numeric_text
            included.extend(words)
            # Attach a nearby textual measure (e.g. "sets") to the first
            # numeric lane using its observed boundary, never a unit list.
            if index == 0:
                right_bound = boundaries[0] if boundaries else float(words[0]["x1"]) + 18.0
                measure_words = [
                    measure_word
                    for run in candidate["line"].get("runs", [])
                    for measure_word in run.get("words", [])
                    if not _is_numeric_token(str(measure_word.get("text", "")))
                    and float(words[0]["x1"]) <= float(measure_word["x0"]) < right_bound
                ]
                if measure_words:
                    suffix = " ".join(str(word["text"]).strip() for word in sorted(measure_words, key=lambda item: float(item["x0"])))
                    values[index + 1] = f"{numeric_text} {suffix}".strip()
                    included.extend(measure_words)
        if sum(bool(value) for value in values) < 4:
            return None
        rows.append(values)
        row_words.append(included)
        row_boxes.append([
            min(float(word["x0"]) for word in included),
            min(float(word["y0"]) for word in included),
            max(float(word["x1"]) for word in included),
            max(float(word["y1"]) for word in included),
        ])

    first_y = min(box[1] for box in row_boxes)
    header_labels: list[tuple[int, dict[str, Any]]] = []
    for line in visual_lines:
        for run in line.get("runs", []):
            text = str(run.get("text", "")).strip()
            y = (float(run.get("y0", 0.0)) + float(run.get("y1", 0.0))) / 2.0
            if not (0 < first_y - y <= 30.0) or len(text) > 40:
                continue
            if not re.search(r"[A-Za-zÀ-ž]", text) or any(
                _is_numeric_token(str(word.get("text", ""))) for word in run.get("words", [])
            ):
                continue
            for lane_index, lane in enumerate(lanes):
                if abs(float(run.get("x0", 0.0)) - lane) <= 24.0:
                    header_labels.append((lane_index, run))
                    break

    headers = [""] * (len(lanes) + 1)
    header_cells: list[dict[str, Any]] = []
    for lane_index, run in header_labels:
        column = lane_index + 1
        text = str(run.get("text", "")).strip()
        if headers[column] and text not in headers[column]:
            headers[column] = f"{headers[column]} {text}".strip()
        else:
            headers[column] = text
    for column, text in enumerate(headers):
        if not text:
            continue
        runs = [run for lane_index, run in header_labels if lane_index + 1 == column]
        header_cells.append({
            "row": 0, "column": column, "text": text,
            "rowspan": 1, "colspan": 1,
            "source_bbox": [
                min(float(run["x0"]) for run in runs),
                min(float(run["y0"]) for run in runs),
                max(float(run["x1"]) for run in runs),
                max(float(run["y1"]) for run in runs),
            ] if runs else None,
        })

    source_index = getattr(page, "number", 0)
    header_bbox = None
    if header_labels:
        label_runs = [run for _lane_index, run in header_labels]
        header_bbox = [
            min(float(run["x0"]) for run in label_runs),
            min(float(run["y0"]) for run in label_runs),
            max(float(run["x1"]) for run in label_runs),
            max(float(run["y1"]) for run in label_runs),
        ]
    if rows:
        source_box = [
            min(box[0] for box in row_boxes),
            min([box[1] for box in row_boxes] + ([header_bbox[1]] if header_bbox else [])),
            max(box[2] for box in row_boxes),
            max(box[3] for box in row_boxes),
        ]
    else:
        return None

    return {
        "table_index": 1,
        "source_page": int(source_index) + 1,
        "table_role": "line_items",
        "layout_type": "item_table",
        "is_source_table": True,
        "headers": headers,
        "header_rows": [headers] if any(headers) else [],
        "header_cells": header_cells,
        "header_detected": False,
        "header_source": "partial_observed_labels" if header_cells else "unavailable",
        "column_count": len(headers),
        "row_count": len(rows),
        "rows": rows,
        "source_bbox": source_box,
        "header_source_bbox": header_bbox,
        "header_source_bboxes": [header_bbox] if header_bbox else [],
        "row_source_bboxes": row_boxes,
        "cell_review_flags": [[True] * len(headers) for _ in rows],
        "rows_need_review": [True] * len(rows),
        "row_review_reasons": [["headerless_repeated_numeric_lanes"] for _ in rows],
        "review_required": True,
        "review_issues": [{
            "code": "header_not_detected",
            "message": "Baris tabel ditemukan dari lane OCR yang berulang; sebagian header tidak tersedia dan perlu diperiksa.",
            "source_bbox": source_box,
        }],
        "detection_strategy": "ocr_repeated_numeric_lanes",
        "row_recovery_strategy": "ocr_repeated_numeric_lanes",
        "detection_confidence": 0.62,
        "context_rows": [],
        "parent_item_context": None,
        "row_parent_links": [],
    }


def _table_has_broken_numeric_row_sequence(table: dict[str, Any]) -> bool:
    """Return whether current OCR rows show a merge/gap pattern worth recovery."""
    identifiers: list[int] = []
    for row in table.get("rows", []):
        if not row:
            continue
        match = re.match(r"\s*(\d+)[.)]?\b", _clean_cell(row[0]))
        if match:
            identifiers.append(int(match.group(1)))
    if len(identifiers) < 4:
        return False
    return any(right <= left or right - left > 2 for left, right in zip(identifiers, identifiers[1:]))



def _extract_ocr_rows_by_quantity_anchors(
    page: Any,
    table: dict[str, Any],
) -> dict[str, Any] | None:
    """Rebuild OCR table rows using the repeated quantity-column lane.

    The geometry-header extractor can produce an overly short source bounding
    box when wrapped descriptions merge into neighbours. Do not use that box to
    restrict OCR searches: detect the item-number lane across the page, select
    the strongest repeated numeric lane to its right, then recover row bands
    from the shared vertical rhythm. Text is preserved as recognized by OCR.
    """
    try:
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        raw_words = page.get_text("words", sort=True)
    except Exception:
        logger.exception("Unable to read OCR words for quantity-anchor recovery")
        return None
    if page_width <= 0 or page_height <= 0:
        return None

    words: list[dict[str, Any]] = []
    for raw in raw_words:
        if len(raw) < 5:
            continue
        text = _clean_cell(raw[4]).strip()
        if not text:
            continue
        try:
            x0, y0, x1, y1 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        except (TypeError, ValueError, OverflowError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        words.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "cx": cx, "cy_raw": cy, "cy": cy, "text": text,
            "page_x": cx / page_width,
            "page_y": cy / page_height,
        })
    if len(words) < 12:
        return None

    id_re = re.compile(r"^(\d{1,3})[.)]?$|^[-–—]$")
    numeric_re = re.compile(r"^(?:\(?-?\d[\d,.]*\)?|[-–—])$")

    # Item numbers live in the leftmost table lane. Ignore the page header and
    # footer initially, then choose the strongest vertically contiguous cluster.
    id_candidates = [
        word for word in words
        if 0.12 <= word["page_x"] <= 0.29
        and 0.10 <= word["page_y"] <= 0.93
        and id_re.fullmatch(word["text"])
        and word["text"].strip(".)-–—").isdigit()
    ]
    id_candidates.sort(key=lambda word: (word["cy_raw"], word["x0"]))
    id_clusters: list[list[dict[str, Any]]] = []
    id_cluster_gap = max(30.0, min(44.0, page_height * 0.05))
    for word in id_candidates:
        if not id_clusters or word["cy_raw"] - id_clusters[-1][-1]["cy_raw"] > id_cluster_gap:
            id_clusters.append([word])
        else:
            id_clusters[-1].append(word)
    if not id_clusters:
        return None
    id_cluster = max(id_clusters, key=lambda group: (len(group), group[-1]["cy_raw"] - group[0]["cy_raw"]))
    if len(id_cluster) < 4:
        return None

    # Collapse duplicated boxes in the number lane without collapsing separate
    # rows that happen to share a digit (e.g. items 11 and 12).
    deduped_ids: list[dict[str, Any]] = []
    for candidate in id_cluster:
        match_index = next((
            index for index, previous in enumerate(deduped_ids)
            if abs(candidate["cy_raw"] - previous["cy_raw"]) <= 2.5
            and abs(candidate["cx"] - previous["cx"]) <= page_width * 0.025
        ), None)
        if match_index is None:
            copied = dict(candidate)
            stripped = candidate["text"].strip()
            copied["identifier"] = str(int(stripped.rstrip(".)")))
            deduped_ids.append(copied)
        elif candidate["x1"] - candidate["x0"] > deduped_ids[match_index]["x1"] - deduped_ids[match_index]["x0"]:
            copied = dict(candidate)
            copied["identifier"] = str(int(candidate["text"].strip().rstrip(".)")))
            deduped_ids[match_index] = copied
    id_cluster = sorted(deduped_ids, key=lambda word: word["cy_raw"])
    if len(id_cluster) < 4:
        return None

    id_centers_raw = [word["cy_raw"] for word in id_cluster]
    typical_id_gap = float(median([
        gap for gap in (right - left for left, right in zip(id_centers_raw, id_centers_raw[1:]))
        if 3.0 <= gap <= 55.0
    ] or [page_height * 0.02]))
    # Use the observed item-number range to exclude document header/footer digits.
    row_y_min = id_centers_raw[0] - max(5.0, typical_id_gap * 0.65)
    row_y_max = id_centers_raw[-1] + max(5.0, typical_id_gap * 0.65)

    # Candidate quantities are to the right of the item-number lane. Find the
    # densest compact x-cluster rather than trusting source_bbox, which is often
    # clipped when the first table pass has merged rows.
    numeric_candidates = [
        word for word in words
        if 0.48 <= word["page_x"] <= 0.92
        and row_y_min <= word["cy_raw"] <= row_y_max
        and numeric_re.fullmatch(word["text"])
    ]
    if len(numeric_candidates) < 4:
        return None
    numeric_candidates.sort(key=lambda word: word["cx"])
    x_window = max(10.0, page_width * 0.028)
    best_group: list[dict[str, Any]] = []
    right = 0
    for left in range(len(numeric_candidates)):
        while right < len(numeric_candidates) and numeric_candidates[right]["cx"] - numeric_candidates[left]["cx"] <= x_window:
            right += 1
        group = numeric_candidates[left:right]
        # Prefer many distinct vertical anchors, with modest preference for
        # compact x grouping; this separates a printed quantity column from a
        # handwritten date or stamp further to the right.
        distinct_y: list[float] = []
        for item in sorted(group, key=lambda token: token["cy_raw"]):
            if not distinct_y or abs(item["cy_raw"] - distinct_y[-1]) > 2.6:
                distinct_y.append(item["cy_raw"])
        if (len(distinct_y), -((group[-1]["cx"] - group[0]["cx"]) if group else 1e6)) > (
            len({round(item["cy_raw"] / 2.6) for item in best_group}),
            -((best_group[-1]["cx"] - best_group[0]["cx"]) if best_group else 1e6),
        ):
            best_group = group
    if len(best_group) < 4:
        return None
    quantity_x = float(median(word["cx"] for word in best_group))
    lane_tolerance = max(8.0, page_width * 0.022)
    quantity_candidates = [
        word for word in numeric_candidates
        if abs(word["cx"] - quantity_x) <= lane_tolerance
    ]
    quantity_candidates.sort(key=lambda word: (word["cy_raw"], word["x0"]))

    # Estimate page skew from paired left identifiers and right quantities.
    # This makes their row centers comparable on lightly rotated scans.
    pair_slopes: list[float] = []
    unused_quantities = set(range(len(quantity_candidates)))
    pair_tolerance = max(4.0, min(12.0, typical_id_gap * 0.55))
    for identifier in id_cluster:
        choices = [
            (abs(quantity_candidates[index]["cy_raw"] - identifier["cy_raw"]), index)
            for index in unused_quantities
        ]
        if not choices:
            break
        distance, index = min(choices)
        quantity = quantity_candidates[index]
        dx = quantity["cx"] - identifier["cx"]
        if distance <= pair_tolerance and dx >= page_width * 0.30:
            slope = (quantity["cy_raw"] - identifier["cy_raw"]) / dx
            if abs(slope) <= 0.05:
                pair_slopes.append(slope)
                unused_quantities.remove(index)
    skew_slope = float(median(pair_slopes)) if len(pair_slopes) >= 3 else 0.0
    for word in words:
        word["cy"] = word["cy_raw"] - skew_slope * word["cx"]
    for word in id_cluster + quantity_candidates:
        word["cy"] = word["cy_raw"] - skew_slope * word["cx"]

    # De-duplicate overlapping boxes for the same quantity token.
    quantities: list[dict[str, Any]] = []
    for candidate in sorted(quantity_candidates, key=lambda word: word["cy"]):
        duplicate = next((
            previous for previous in reversed(quantities[-3:])
            if abs(candidate["cy"] - previous["cy"]) <= 2.6
            and abs(candidate["cx"] - previous["cx"]) <= lane_tolerance
        ), None)
        if duplicate is None:
            quantities.append(candidate)
        elif len(candidate["text"]) > len(duplicate["text"]):
            quantities[quantities.index(duplicate)] = candidate
    if len(quantities) < 4:
        return None

    qty_centers = [float(word["cy"]) for word in quantities]
    gaps = [right - left for left, right in zip(qty_centers, qty_centers[1:]) if 2.5 <= right - left <= 55.0]
    typical_qty_gap = float(median(gaps or [typical_id_gap]))
    # Keep the contiguous run of item rows. A genuine table has a repeated row
    # rhythm; stray stamp digits/header numbers are separated by larger gaps.
    max_cluster_gap = max(typical_qty_gap * 2.4, min(45.0, typical_qty_gap + 18.0))
    qty_clusters: list[list[dict[str, Any]]] = []
    for quantity in quantities:
        if not qty_clusters or quantity["cy"] - qty_clusters[-1][-1]["cy"] > max_cluster_gap:
            qty_clusters.append([quantity])
        else:
            qty_clusters[-1].append(quantity)
    qty_cluster = max(qty_clusters, key=lambda group: (len(group), group[-1]["cy"] - group[0]["cy"]))
    if len(qty_cluster) < 4:
        return None

    centers = [float(item["cy"]) for item in qty_cluster]
    if any(right <= left for left, right in zip(centers, centers[1:])):
        return None
    boundaries = [centers[0] - (centers[1] - centers[0]) / 2.0]
    boundaries.extend((left + right) / 2.0 for left, right in zip(centers, centers[1:]))
    boundaries.append(centers[-1] + (centers[-1] - centers[-2]) / 2.0)

    id_x_max = float(median(word["x1"] for word in id_cluster)) + max(2.0, page_width * 0.006)
    desc_x_max = quantity_x - max(3.0, page_width * 0.008)
    if desc_x_max <= id_x_max:
        return None
    # Use closest row-number anchors only when a number is actually detected;
    # never invent missing sequential IDs because source documents may skip IDs.
    row_values: list[list[str]] = []
    row_bboxes: list[list[float]] = []
    for index, quantity in enumerate(qty_cluster):
        top, bottom = boundaries[index], boundaries[index + 1]
        row_ids = [word for word in id_cluster if top <= word["cy"] <= bottom]
        identifier = ""
        if row_ids:
            closest_id = min(row_ids, key=lambda word: abs(word["cy"] - quantity["cy"]))
            identifier = closest_id["identifier"]

        desc_words = [
            word for word in words
            if id_x_max <= word["x0"] < desc_x_max
            and top <= word["cy"] <= bottom
            and word not in id_cluster
            and word not in qty_cluster
        ]
        desc_words.sort(key=lambda word: (round(word["cy"], 1), word["x0"]))
        deduped: list[dict[str, Any]] = []
        for word in desc_words:
            if any(
                word["text"].casefold() == previous["text"].casefold()
                and abs(word["cy"] - previous["cy"]) <= 2.0
                and abs(word["x0"] - previous["x0"]) <= 2.5
                for previous in deduped[-15:]
            ):
                continue
            deduped.append(word)
        description = re.sub(r"\s+", " ", " ".join(word["text"] for word in deduped)).strip()
        quantity_text = _clean_cell(quantity["text"]).strip()
        if not (identifier or description) or not quantity_text:
            continue
        row_values.append([identifier, description, quantity_text])
        row_words = [*deduped, quantity]
        if row_ids:
            row_words.extend(row_ids)
        row_bboxes.append([
            round(min(word["x0"] for word in row_words), 2),
            round(min(word["y0"] for word in row_words), 2),
            round(max(word["x1"] for word in row_words), 2),
            round(max(word["y1"] for word in row_words), 2),
        ])

    if len(row_values) < 4:
        return None
    described_rows = sum(bool(row[1]) for row in row_values)
    if described_rows < max(3, int(len(row_values) * 0.70)):
        return None

    return {
        "rows": row_values,
        "row_bboxes": row_bboxes,
        "row_count": len(row_values),
        "source_bbox": [
            round(min(box[0] for box in row_bboxes), 2),
            round(min(box[1] for box in row_bboxes), 2),
            round(max(box[2] for box in row_bboxes), 2),
            round(max(box[3] for box in row_bboxes), 2),
        ],
        "recovery_strategy": "ocr_quantity_anchor_bands",
        "skew_slope_correction": round(skew_slope, 5),
        "detection_confidence": round(min(0.92, 0.58 + len(row_values) * 0.014), 2),
    }


def _extract_ocr_rows_by_horizontal_rules(
    page: Any,
    table: dict[str, Any],
) -> dict[str, Any] | None:
    """Rebuild a ruled OCR table using detected horizontal cell boundaries.

    This fallback is intentionally used only for OCR pages with an observed
    quantity heading and a strong set of nearly-horizontal rules. It assigns
    words to each physical row band before joining wrapped description lines,
    so text from adjacent products cannot be merged merely because the OCR
    baselines or quantity anchors were imperfect.
    """
    if not getattr(page, "ocr_word_count", 0):
        return None

    headers = [_clean_cell(value) for value in table.get("headers", [])]
    if len(headers) < 3 or not re.search(
        r"\b(?:qty|quantity)\b", headers[-1], flags=re.IGNORECASE
    ):
        return None

    try:
        import cv2
        import numpy as np
        import pymupdf

        page_rect = page.rect
        page_width = float(page_rect.width)
        page_height = float(page_rect.height)
        if page_width <= 0 or page_height <= 0:
            return None

        # OCRPageProxy forwards get_pixmap() to the original PDF page, letting
        # us inspect actual table rules while continuing to use OCR word boxes.
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(150 / 72, 150 / 72),
            alpha=False,
        )
        image_width = int(pixmap.width)
        image_height = int(pixmap.height)
        if image_width < 200 or image_height < 200:
            return None

        samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
        samples = samples.reshape(image_height, image_width, pixmap.n)
        if pixmap.n == 1:
            gray = samples[:, :, 0]
        elif pixmap.n == 4:
            gray = cv2.cvtColor(samples, cv2.COLOR_RGBA2GRAY)
        else:
            gray = cv2.cvtColor(samples[:, :, :3], cv2.COLOR_RGB2GRAY)

        raw_words = page.get_text("words", sort=True)
    except Exception:
        # OpenCV is an optional enhancement. Keep the existing OCR fallback if
        # raster-line analysis is unavailable; do not fail document inspection.
        logger.debug("OCR horizontal-rule recovery unavailable", exc_info=True)
        return None

    if not raw_words:
        return None

    sx = image_width / page_width
    sy = image_height / page_height
    words: list[dict[str, Any]] = []
    for raw in raw_words:
        if len(raw) < 5:
            continue
        text = _clean_cell(raw[4])
        if not text:
            continue
        try:
            x0 = (float(raw[0]) - float(page_rect.x0)) * sx
            y0 = (float(raw[1]) - float(page_rect.y0)) * sy
            x1 = (float(raw[2]) - float(page_rect.x0)) * sx
            y1 = (float(raw[3]) - float(page_rect.y0)) * sy
        except (TypeError, ValueError, OverflowError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        words.append({
            "text": text,
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "cx": (x0 + x1) / 2.0,
            "cy": (y0 + y1) / 2.0,
        })
    if len(words) < 20:
        return None

    # Find the actual source word 'Quantity'/'Qty' near the top of the page.
    # Do not run this scan-specific fallback when the page has no printed
    # quantity heading (for example, the continuation page of the test PDF).
    quantity_headers = []
    for word in words:
        token = re.sub(r"[^a-z0-9]", "", word["text"].casefold())
        if not (token in {"qty", "qnty", "quantity", "quantities"} or token.startswith("quantit")):
            continue
        if word["cx"] < image_width * 0.40 or word["cy"] > image_height * 0.45:
            continue
        quantity_headers.append(word)
    if not quantity_headers:
        return None
    quantity_header = min(quantity_headers, key=lambda item: item["cy"])

    edges = cv2.Canny(gray, 30, 100, apertureSize=3)
    line_data = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(45, int(image_width * 0.035)),
        minLineLength=max(50, int(image_width * 0.09)),
        maxLineGap=max(12, int(image_width * 0.045)),
    )
    segments: list[tuple[float, float, float, float, float, float]] = []
    # HoughLinesP returns (N, 1, 4) on most OpenCV builds but (N, 4) on others;
    # normalise the shape so both work.
    hough_rows = np.asarray(line_data).reshape(-1, 4) if line_data is not None else []
    for raw in hough_rows:
        x0, y0, x1, y1 = map(float, raw)
        if x1 < x0:
            x0, x1, y0, y1 = x1, x0, y1, y0
        dx = x1 - x0
        if dx < image_width * 0.08:
            continue
        slope = (y1 - y0) / max(dx, 1.0)
        if abs(slope) > 0.06:
            continue
        segments.append((slope, x0, x1, y0, y1, dx))

    long_slopes = [segment[0] for segment in segments if segment[5] >= image_width * 0.35]
    if len(long_slopes) < 4:
        return None
    page_slope = float(median(long_slopes))

    line_candidates: list[tuple[float, float, float]] = []
    for slope, x0, x1, y0, y1, dx in segments:
        if abs(slope - page_slope) > 0.025:
            continue
        # The visible rules must span the left side through the right side of
        # the same table. This excludes short text strokes and most stamp marks.
        if x0 > image_width * 0.30 or x1 < image_width * 0.60 or dx < image_width * 0.40:
            continue
        intercept = ((y0 + y1) / 2.0) - page_slope * ((x0 + x1) / 2.0)
        line_candidates.append((intercept, x0, x1))
    if len(line_candidates) < 8:
        return None
    line_candidates.sort(key=lambda item: item[0])

    grouped_lines: list[list[tuple[float, float, float]]] = []
    for line in line_candidates:
        if not grouped_lines or line[0] - median([entry[0] for entry in grouped_lines[-1]]) > 4.5:
            grouped_lines.append([line])
        else:
            grouped_lines[-1].append(line)

    rules: list[dict[str, float]] = []
    for group in grouped_lines:
        intervals = sorted((entry[1], entry[2]) for entry in group)
        if not intervals:
            continue
        covered = 0.0
        current_x0, current_x1 = intervals[0]
        for next_x0, next_x1 in intervals[1:]:
            if next_x0 <= current_x1 + 10.0:
                current_x1 = max(current_x1, next_x1)
            else:
                covered += current_x1 - current_x0
                current_x0, current_x1 = next_x0, next_x1
        covered += current_x1 - current_x0
        min_x = min(entry[1] for entry in group)
        max_x = max(entry[2] for entry in group)
        if covered < image_width * 0.40 or min_x > image_width * 0.25 or max_x < image_width * 0.60:
            continue
        rules.append({
            "y": float(median([entry[0] for entry in group])),
            "x0": min_x,
            "x1": max_x,
            "coverage": covered,
        })
    rules.sort(key=lambda item: item["y"])

    quantity_header_y = quantity_header["cy"]
    # The header word sits between two rules; the first rule below its centre
    # is the bottom of the header row and the top of the first data row.
    lower_header_rules = [rule for rule in rules if rule["y"] > quantity_header_y + 1.0]
    if not lower_header_rules:
        return None
    first_data_boundary = min(lower_header_rules, key=lambda item: item["y"])

    # Restrict recovery to the ruled table area. The footer/signature lies below
    # the row grid in the tested layout and must not become an item row.
    body_rules = [
        rule for rule in rules
        if rule["y"] >= first_data_boundary["y"] - 1.0
        and rule["y"] < image_height * 0.84
    ]
    if len(body_rules) < 6:
        return None

    normal_gaps = [
        right["y"] - left["y"]
        for left, right in zip(body_rules, body_rules[1:])
        if 18.0 <= right["y"] - left["y"] <= 90.0
    ]
    typical_gap = float(median(normal_gaps or [40.0]))
    near_rule_threshold = max(7.0, typical_gap * 0.45)
    rule_groups: list[list[dict[str, float]]] = []
    for rule in body_rules:
        if not rule_groups or rule["y"] - rule_groups[-1][-1]["y"] >= near_rule_threshold:
            rule_groups.append([rule])
        else:
            rule_groups[-1].append(rule)

    # A pen strike-through can create a second long line very close to a real
    # grid rule. Choose the member that makes the neighbouring row heights most
    # consistent, rather than choosing the visually strongest scribble.
    merged_rules: list[dict[str, float]] = []
    for group_index, group in enumerate(rule_groups):
        if len(group) == 1:
            merged_rules.append(group[0])
            continue
        previous_y = merged_rules[-1]["y"] if merged_rules else None
        next_y = rule_groups[group_index + 1][0]["y"] if group_index + 1 < len(rule_groups) else None

        def rule_score(rule: dict[str, float]) -> tuple[float, float]:
            if previous_y is not None and next_y is not None:
                balance = abs((rule["y"] - previous_y) - (next_y - rule["y"]))
            elif previous_y is not None:
                balance = abs((rule["y"] - previous_y) - typical_gap)
            elif next_y is not None:
                balance = abs((next_y - rule["y"]) - typical_gap)
            else:
                balance = 0.0
            return (balance, -rule["coverage"])

        merged_rules.append(min(group, key=rule_score))

    if len(merged_rules) < 6:
        return None

    # Use the observed first-column/quantity-header geometry rather than fixed
    # column labels or vendor coordinates.
    table_left = float(median([rule["x0"] for rule in merged_rules]))
    table_right = float(median([rule["x1"] for rule in merged_rules]))
    table_width = table_right - table_left
    if table_width < image_width * 0.30:
        return None
    id_cutoff = table_left + table_width * 0.11
    quantity_x = quantity_header["cx"]
    quantity_tolerance = max(image_width * 0.025, 18.0)
    description_x0 = id_cutoff + 1.0
    description_x1 = quantity_x - max(image_width * 0.032, 18.0)
    if description_x1 <= description_x0:
        return None

    # Row boundaries are in skew-corrected raster coordinates; OCRPageProxy
    # exposes word boxes corrected using the same page geometry and scaled here.
    row_values: list[list[str]] = []
    row_bboxes: list[list[float]] = []
    rows_need_review: list[bool] = []
    all_row_band_bboxes: list[list[float]] = []
    number_re = re.compile(r"^\d{1,3}[.)]?$")
    quantity_re = re.compile(r"^(?:\(?-?\d[\d,.]*\)?|[-–—])$")

    for top_rule, bottom_rule in zip(merged_rules, merged_rules[1:]):
        top_y, bottom_y = top_rule["y"], bottom_rule["y"]
        if bottom_y - top_y < 15.0:
            continue
        row_words = [
            word for word in words
            if top_y - 2.0 <= word["cy"] < bottom_y + 1.0
            and word["cx"] >= table_left - 5.0
            and word["cx"] <= table_right + 8.0
        ]

        identifiers = [
            word for word in row_words
            if word["cx"] <= id_cutoff and number_re.fullmatch(word["text"].strip())
        ]
        # Prefer a number near the centre of the detected identifier lane and
        # near the vertical centre of this particular physical row.
        identifier_word = min(
            identifiers,
            key=lambda word: (
                abs(word["cx"] - (table_left + table_width * 0.055)),
                abs(word["cy"] - (top_y + bottom_y) / 2.0),
            ),
            default=None,
        )
        identifier = _clean_cell(identifier_word["text"]).rstrip(".)") if identifier_word else ""

        quantity_words = [
            word for word in row_words
            if abs(word["cx"] - quantity_x) <= quantity_tolerance
            and quantity_re.fullmatch(word["text"].strip())
        ]
        quantity_word = min(
            quantity_words,
            key=lambda word: (
                abs(word["cx"] - quantity_x),
                abs(word["cy"] - (top_y + bottom_y) / 2.0),
            ),
            default=None,
        )
        quantity = _clean_cell(quantity_word["text"]).strip() if quantity_word else ""
        if quantity and re.fullmatch(r"\d+\.", quantity):
            quantity = quantity[:-1]

        description_words = [
            word for word in row_words
            if description_x0 <= word["cx"] < description_x1
            and word is not identifier_word
            and word is not quantity_word
        ]
        description_words.sort(key=lambda word: (word["cy"], word["x0"]))
        text_lines: list[dict[str, Any]] = []
        line_tolerance = 5.5
        for word in description_words:
            if not text_lines or abs(word["cy"] - text_lines[-1]["cy"]) > line_tolerance:
                text_lines.append({"cy": word["cy"], "words": [word]})
            else:
                text_lines[-1]["words"].append(word)
                text_lines[-1]["cy"] = float(median([item["cy"] for item in text_lines[-1]["words"]]))
        description_parts = [
            " ".join(_clean_cell(word["text"]) for word in sorted(line["words"], key=lambda item: item["x0"]))
            for line in text_lines
        ]
        description = re.sub(r"\s+", " ", " ".join(part for part in description_parts if part)).strip()

        # Every physical grid band represents one source row, even if OCR misses
        # its contents. Missing cells stay blank and receive a review flag; the
        # extractor never invents an item number or quantity.
        row_values.append([identifier, description, quantity])
        row_needs_review = not (identifier and description and quantity)
        rows_need_review.append(row_needs_review)

        contributing = [word for word in row_words if word is identifier_word or word is quantity_word or word in description_words]
        if contributing:
            bbox = [
                min(word["x0"] for word in contributing) / sx + float(page_rect.x0),
                min(word["y0"] for word in contributing) / sy + float(page_rect.y0),
                max(word["x1"] for word in contributing) / sx + float(page_rect.x0),
                max(word["y1"] for word in contributing) / sy + float(page_rect.y0),
            ]
        else:
            bbox = [
                table_left / sx + float(page_rect.x0),
                top_y / sy + float(page_rect.y0),
                table_right / sx + float(page_rect.x0),
                bottom_y / sy + float(page_rect.y0),
            ]
        row_bboxes.append([round(float(value), 2) for value in bbox])
        all_row_band_bboxes.append([
            round(table_left / sx + float(page_rect.x0), 2),
            round(top_y / sy + float(page_rect.y0), 2),
            round(table_right / sx + float(page_rect.x0), 2),
            round(bottom_y / sy + float(page_rect.y0), 2),
        ])

    if len(row_values) < 4:
        return None
    described = sum(bool(row[1]) for row in row_values)
    if described < max(3, int(len(row_values) * 0.70)):
        return None

    return {
        "rows": row_values,
        "row_bboxes": row_bboxes,
        "row_band_bboxes": all_row_band_bboxes,
        "rows_need_review": rows_need_review,
        "row_count": len(row_values),
        "source_bbox": [
            round(table_left / sx + float(page_rect.x0), 2),
            round(merged_rules[0]["y"] / sy + float(page_rect.y0), 2),
            round(table_right / sx + float(page_rect.x0), 2),
            round(merged_rules[-1]["y"] / sy + float(page_rect.y0), 2),
        ],
        "recovery_strategy": "ocr_horizontal_rule_row_bands",
        "horizontal_rule_count": len(merged_rules),
        "detected_page_slope": round(page_slope, 5),
        "detection_confidence": round(min(0.96, 0.72 + described / max(1, len(row_values)) * 0.20), 2),
    }


def extract_tables_from_page(
    page: Any,
    *,
    timing_metrics: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return distinct source tables, one metadata table, and document summaries.

    ``tables`` contains the detected PDF table regions plus at most one logical
    label/value table generated from positioned document metadata. The logical
    table is marked ``is_source_table=False`` so it is not counted as a native
    table. Existing response fields are preserved for frontend compatibility.
    """
    methods_attempted: list[str] = []
    methods_failed: list[str] = []
    detection_started = time.perf_counter()
    candidates = _native_table_candidates(
        page, methods_attempted, methods_failed, strategies=("lines",)
    )
    if timing_metrics is not None:
        timing_metrics["native_table_detection_seconds"] = time.perf_counter() - detection_started
    reconstruction_started = time.perf_counter()

    line_candidates = [candidate for candidate in candidates if candidate["strategy"] == "lines"]
    source_tables, data_bottom = _extract_from_native_candidates(line_candidates)

    visual_lines = _word_runs_on_visual_baselines(page)
    if getattr(page, "ocr_word_count", 0):
        visual_lines = _without_browser_print_footer_lines(
            visual_lines, float(page.rect.height)
        )
    positioned_summary_candidates: list[dict[str, Any]] | None = None
    line_item_tables = [
        table for table in source_tables
        if table.get("table_role") != "document_header"
    ]
    line_tables_have_headers = any(table.get("header_detected") for table in line_item_tables)
    largest_native_candidate = max(
        (int(table.get("row_count") or 0) for table in line_item_tables),
        default=0,
    )
    native_candidate_has_repeated_item_ids = any(
        sum(
            bool(row)
            and _is_item_identifier(_clean_cell(row[0]))
            and sum(bool(_clean_cell(value)) for value in row) >= 2
            for row in table.get("rows", [])
            if isinstance(row, (list, tuple))
        ) >= 2
        for table in line_item_tables
    )
    # A small native candidate can be a key/value block above a borderless item
    # table. Its label row may look like a table header, so also consult the
    # page's positioned row geometry before allowing it to suppress recovery.
    # Do not run this extra check when PyMuPDF has already split a source grid
    # into multiple row fragments; the existing headerless recovery handles
    # those pages and avoids duplicating their native fragments.
    native_candidate_is_small = (
        not getattr(page, "ocr_word_count", 0)
        and len(line_item_tables) == 1
        and largest_native_candidate < 3
        and not native_candidate_has_repeated_item_ids
    )
    if not source_tables or not line_tables_have_headers or native_candidate_is_small:
        positioned_summary_candidates = _find_summary_candidates(page, visual_lines)
        geometry_table, geometry_bottom = _extract_geometry_table(
            page, visual_lines, positioned_summary_candidates,
        )
        if geometry_table:
            geometry_is_stronger = (
                bool(geometry_table.get("header_detected"))
                and int(geometry_table.get("row_count") or 0) > largest_native_candidate
            )
            if not source_tables:
                source_tables.append(geometry_table)
                data_bottom = geometry_bottom
            elif geometry_is_stronger:
                if not line_tables_have_headers:
                    # Preserve the established behavior for headerless pages:
                    # a stronger geometry reconstruction replaces weak native
                    # fragments instead of appearing as an additional table.
                    source_tables = [geometry_table]
                else:
                    target_indexes = [
                        index for index, table in enumerate(source_tables)
                        if table.get("table_role") != "document_header"
                    ]
                    if target_indexes:
                        target_index = max(
                            target_indexes,
                            key=lambda index: int(source_tables[index].get("row_count") or 0),
                        )
                        source_tables[target_index] = geometry_table
                    else:
                        source_tables.append(geometry_table)
                data_bottom = geometry_bottom
            if "geometry_header_anchored" not in methods_attempted:
                methods_attempted.append("geometry_header_anchored")

    # A page may contain a clearly ruled table header but no item rows (for
    # example, a continuation or intentionally empty order page). Preserve the
    # source headings only when the geometry itself brackets that header.
    # Existing row-backed native and geometry extraction always takes priority.
    if not source_tables:
        header_only_table = _extract_ruled_header_only_table(page, visual_lines)
        if header_only_table is not None:
            source_tables = [header_only_table]
            data_bottom = float(header_only_table["source_bbox"][3])
            methods_attempted.append("native_ruled_header_only")

    # OCR word boxes can fragment a true column heading or fail to retain its
    # baseline. In that case reconstruct the item rows from repeated, aligned
    # identifiers and the rightmost numeric lane, then use the nearby source
    # header labels. Prefer it over a weak header-anchored candidate when it
    # yields more rows or a header much closer to the first detected item row.
    # OCR-only fallback. Do not run this on native digital PDF pages: their
    # ruled-table extraction can already provide all columns, while this
    # heuristic may collapse several right-side columns into one quantity lane.
    repeated_table = None
    repeated_bottom = None
    if getattr(page, "ocr_word_count", 0):
        repeated_table, repeated_bottom = _extract_repeated_identifier_table(page, visual_lines)
    if repeated_table:
        existing = source_tables[0] if source_tables else None
        existing_rows = int(existing.get("row_count", 0)) if existing else 0
        existing_header = existing.get("header_source_bbox") if existing else None
        existing_first_row = (existing.get("row_source_bboxes") or [None])[0] if existing else None
        existing_gap = None
        if existing_header and existing_first_row and len(existing_header) == 4 and len(existing_first_row) == 4:
            existing_gap = float(existing_first_row[1]) - float(existing_header[3])
        repeated_gap = repeated_table.pop("_header_distance", None)
        repeated_better_header = (
            bool(repeated_table.get("header_detected"))
            and repeated_gap is not None
            and (existing_gap is None or existing_gap - repeated_gap >= 8.0)
        )
        repeated_wider_with_row_support = (
            existing is not None
            and int(repeated_table.get("column_count") or 0)
            > int(existing.get("column_count") or len(existing.get("headers", [])))
            and int(repeated_table["row_count"]) >= max(2, int(existing_rows * 0.9))
        )
        if (
            existing is None
            or int(repeated_table["row_count"]) > existing_rows
            or repeated_better_header
            or repeated_wider_with_row_support
        ):
            source_tables = [repeated_table]
            data_bottom = repeated_bottom
            methods_attempted.append("ocr_repeated_row_geometry")
        elif "_header_distance" in repeated_table:
            repeated_table.pop("_header_distance", None)

    if not source_tables:
        # PyMuPDF's text strategy may partition prose into false tables. Use it
        # only after the line and geometry paths failed, and only when a compact
        # header has repeated, credible item identifiers. Previously this finder
        # ran on every page even though its results were discarded whenever an
        # earlier path found a table.
        text_detection_started = time.perf_counter()
        text_candidates = _native_table_candidates(
            page, methods_attempted, methods_failed, strategies=("text",)
        )
        candidates.extend(text_candidates)
        if timing_metrics is not None:
            timing_metrics["native_table_detection_seconds"] = (
                timing_metrics.get("native_table_detection_seconds", 0.0)
                + time.perf_counter() - text_detection_started
            )
        text_candidates = []
        for candidate in candidates:
            if candidate["strategy"] != "text":
                continue
            rows = candidate["rows"]
            header_index = _find_header_index(rows)
            if header_index is None or header_index >= len(rows) - 1:
                continue
            data_rows = rows[header_index + 1:]
            credible_rows = [
                row for row in data_rows
                if sum(bool(_clean_cell(cell)) for cell in row) >= 2
                and not any(len(_clean_cell(cell)) > 80 for cell in row if cell)
            ]
            indexed_rows = [
                row for row in credible_rows
                if row and _is_item_identifier(_clean_cell(row[0]))
            ]
            if len(indexed_rows) >= 2:
                text_candidates.append(candidate)
        source_tables, data_bottom = _extract_from_native_candidates(text_candidates)

    source_tables = _merge_native_row_fragments(source_tables)

    # Some scanned tables expose the first two source headers as one geometry
    # column (for example, "Line No. Item") even though item identifiers are
    # visible as a compact prefix in each extracted row. Split that merged
    # source-derived header and prefix into separate columns when the evidence
    # is consistent; do not force this transformation on unrelated tables.
    for table in source_tables:
        _split_merged_identifier_description_table(table)

    # When the existing OCR reconstruction has merged neighbouring items, use
    # the page's own header runs and repeated identifier lane to rebuild the
    # complete dynamic row shape. Replace only when the source anchors recover
    # strictly more rows than the current candidate.
    if getattr(page, "ocr_word_count", 0):
        current_table = next((table for table in source_tables if table.get("table_role", "line_items") != "document_header"), None)
        dynamic_recovery = _extract_ocr_rows_by_dynamic_identifier_anchors(
            page, visual_lines, list(current_table.get("headers", [])) if current_table else None,
        )
        if dynamic_recovery:
            target = next((table for table in source_tables if table.get("table_role", "line_items") != "document_header"), None)
            if (
                target is not None
                and _table_has_broken_numeric_row_sequence(target)
                and int(dynamic_recovery["row_count"]) > int(target.get("row_count") or len(target.get("rows", [])))
            ):
                target["headers"] = dynamic_recovery["headers"]
                target["header_rows"] = dynamic_recovery["header_rows"]
                target["header_cells"] = dynamic_recovery["header_cells"]
                target["header_detected"] = True
                target["header_source"] = "detected_from_source_header_geometry"
                target["rows"] = dynamic_recovery["rows"]
                target["row_count"] = dynamic_recovery["row_count"]
                target["column_count"] = len(dynamic_recovery["headers"])
                target["row_source_bboxes"] = dynamic_recovery["row_bboxes"]
                target["source_bbox"] = dynamic_recovery["source_bbox"]
                target["header_source_bbox"] = dynamic_recovery["header_source_bbox"]
                target["row_recovery_strategy"] = dynamic_recovery["row_recovery_strategy"]
                target["rows_need_review"] = dynamic_recovery["rows_need_review"]
                target["detection_confidence"] = max(
                    float(target.get("detection_confidence") or 0.0),
                    float(dynamic_recovery["detection_confidence"]),
                )
                data_bottom = max(float(data_bottom or 0.0), float(dynamic_recovery["source_bbox"][3]))

    # On OCR-backed scans, row numbers are vertically centered inside their
    # table cells while item descriptions may occupy several baselines. Repair
    # merged neighbouring descriptions using item-number row bands, but keep the
    # detected dynamic headers and accept the repair only when enough rows and
    # descriptions have been recovered. Native digital-page extraction is untouched.
    if getattr(page, "ocr_word_count", 0):
        for table in source_tables:
            headers = [_clean_cell(value) for value in table.get("headers", [])]
            if (
                table.get("table_role", "line_items") == "document_header"
                or len(headers) < 3
                or not re.search(r"\b(?:qty|quantity)\b", headers[-1], flags=re.IGNORECASE)
            ):
                continue
            recovered = _extract_ocr_rows_by_vertical_item_anchors(page)
            if not recovered:
                continue
            current_width = int(table.get("column_count") or len(headers))
            recovered_width = max((len(row) for row in recovered.get("rows", [])), default=0)
            if recovered_width < current_width:
                continue
            current_count = int(table.get("row_count") or len(table.get("rows", [])))
            if recovered["row_count"] < current_count:
                continue
            if recovered["row_count"] < max(4, current_count):
                continue
            table["rows"] = recovered["rows"]
            table["row_count"] = recovered["row_count"]
            table["column_count"] = len(headers)
            table["row_source_bboxes"] = recovered["row_bboxes"]
            table["source_bbox"] = recovered["source_bbox"]
            table["row_recovery_strategy"] = recovered["recovery_strategy"]
            table["detection_confidence"] = max(
                float(table.get("detection_confidence") or 0.0),
                recovered["detection_confidence"],
            )
            table["rows_recovered_by_vertical_anchors"] = True
            break

        # Prefer right-column quantity anchors as a second recovery strategy.
        # Unlike line-number anchors, they can retain a row when the item number
        # is crossed out or missed by OCR. This runs after the first fallback so
        # it can replace a weak result with a larger, better-partitioned table.
        for table in source_tables:
            headers = [_clean_cell(value) for value in table.get("headers", [])]
            if (
                table.get("table_role", "line_items") == "document_header"
                or len(headers) < 3
                or not re.search(r"\b(?:qty|quantity)\b", headers[-1], flags=re.IGNORECASE)
            ):
                continue
            recovered = _extract_ocr_rows_by_quantity_anchors(page, table)
            if not recovered:
                continue
            current_width = int(table.get("column_count") or len(headers))
            recovered_width = max((len(row) for row in recovered.get("rows", [])), default=0)
            if recovered_width < current_width:
                continue
            current_count = int(table.get("row_count") or len(table.get("rows", [])))
            if recovered["row_count"] < current_count:
                continue
            table["rows"] = recovered["rows"]
            table["row_count"] = recovered["row_count"]
            table["column_count"] = len(headers)
            table["row_source_bboxes"] = recovered["row_bboxes"]
            table["source_bbox"] = recovered["source_bbox"]
            table["row_recovery_strategy"] = recovered["recovery_strategy"]
            table["skew_slope_correction"] = recovered["skew_slope_correction"]
            table["rows_recovered_by_quantity_anchors"] = True
            table["detection_confidence"] = max(
                float(table.get("detection_confidence") or 0.0),
                recovered["detection_confidence"],
            )
            break

        # Final OCR fallback: use the actual horizontal table rules to create
        # one row per physical cell band. This supersedes anchor-only rows when
        # it recovers at least as many rows and contains enough description text.
        for table in source_tables:
            if table.get("table_role", "line_items") == "document_header":
                continue
            recovered = _extract_ocr_rows_by_horizontal_rules(page, table)
            if not recovered:
                continue
            current_width = int(table.get("column_count") or len(table.get("headers", [])))
            recovered_width = max((len(row) for row in recovered.get("rows", [])), default=0)
            if recovered_width < current_width:
                continue
            current_count = int(table.get("row_count") or len(table.get("rows", [])))
            recovered_described = sum(bool(row[1]) for row in recovered["rows"])
            if recovered["row_count"] < current_count:
                continue
            if recovered_described < max(3, int(recovered["row_count"] * 0.70)):
                continue
            table["rows"] = recovered["rows"]
            table["row_count"] = recovered["row_count"]
            table["column_count"] = len(table.get("headers", [])) or 3
            table["row_source_bboxes"] = recovered["row_bboxes"]
            table["row_band_bboxes"] = recovered["row_band_bboxes"]
            table["rows_need_review"] = recovered["rows_need_review"]
            table["source_bbox"] = recovered["source_bbox"]
            table["row_recovery_strategy"] = recovered["recovery_strategy"]
            table["horizontal_rule_count"] = recovered["horizontal_rule_count"]
            table["detected_page_slope"] = recovered["detected_page_slope"]
            table["rows_recovered_by_horizontal_rules"] = True
            table["detection_confidence"] = recovered["detection_confidence"]
            data_bottom = max(float(data_bottom or 0.0), float(recovered["source_bbox"][3]))
            break

        # Last-resort recovery for source rows that have no item number and no
        # complete header. Only accept a repeated, closely spaced geometry with
        # at least three numeric lanes aligned across multiple descriptions.
        line_item_tables = [
            table for table in source_tables
            if table.get("table_role") != "document_header"
        ]
        current = max(
            line_item_tables,
            key=lambda table: int(table.get("row_count") or 0),
            default=None,
        )
        current_count = int(current.get("row_count") or 0) if current else 0
        if current is None or current_count < 2 or not current.get("header_detected"):
            current_bbox = current.get("source_bbox") if current else None
            after_y = float(current_bbox[3]) if current_bbox and len(current_bbox) == 4 else 0.0
            repeated_measure_table = _extract_ocr_rows_by_repeated_numeric_lanes(
                page, visual_lines, after_y=after_y,
            )
            if repeated_measure_table and int(repeated_measure_table["row_count"]) > current_count:
                if current is not None:
                    old_rows = current.get("rows") or []
                    old_bbox = current.get("source_bbox")
                    if old_rows:
                        parent_row = old_rows[0]
                        parent_text = " ".join(_clean_cell(value) for value in parent_row if _clean_cell(value))
                        identifier_match = re.fullmatch(r"\s*(\d{1,4})[.)]?\s*", _clean_cell(parent_row[0]))
                        description = " ".join(_clean_cell(value) for value in parent_row[1:] if _clean_cell(value))
                        parent_bbox = old_bbox if old_bbox and len(old_bbox) == 4 else None
                        repeated_measure_table["context_rows"] = [{
                            "type": "possible_section_heading",
                            "text": parent_text,
                            "source_bbox": parent_bbox,
                        }]
                        repeated_measure_table["parent_item_context"] = {
                            "identifier": identifier_match.group(1) if identifier_match else None,
                            "description": description,
                            "source_bbox": parent_bbox,
                            "child_item_ids": [],
                            "source_page": int(getattr(page, "number", 0)) + 1,
                            "identifier_needs_review": identifier_match is None,
                        }
                    source_tables[source_tables.index(current)] = repeated_measure_table
                else:
                    source_tables.append(repeated_measure_table)
                data_bottom = max(float(data_bottom or 0.0), float(repeated_measure_table["source_bbox"][3]))
                methods_attempted.append("ocr_repeated_numeric_lanes")

    if getattr(page, "ocr_word_count", 0):
        line_item_tables = [
            table for table in source_tables
            if table.get("table_role") != "document_header"
        ]
        weak_geometry = not line_item_tables or any(
            int(table.get("row_count") or 0) < 2
            or int(table.get("column_count") or len(table.get("headers", []))) < 2
            or (
                not table.get("header_detected")
                and table.get("row_recovery_strategy") != "ocr_repeated_numeric_lanes"
            )
            for table in line_item_tables
        )
        if weak_geometry:
            try:
                from backend.services.rapid_table_fallback import extract_rapid_table_fallback

                structure_table = extract_rapid_table_fallback(page)
            except Exception:  # optional fallback must not affect the geometry result
                logger.debug("RapidTable fallback could not be loaded", exc_info=True)
                structure_table = None
            if structure_table:
                structure_table["is_source_table"] = True
                structure_table["table_role"] = "line_items"
                current = max(
                    line_item_tables,
                    key=lambda table: int(table.get("row_count") or 0),
                    default=None,
                )
                stronger = current is None or (
                    int(structure_table["column_count"]) >= int(
                        current.get("column_count") or len(current.get("headers", []))
                    )
                    and int(structure_table["row_count"]) >= int(current.get("row_count") or 0)
                    and (
                        int(structure_table["column_count"]) > int(
                            current.get("column_count") or len(current.get("headers", []))
                        )
                        or int(structure_table["row_count"]) > int(current.get("row_count") or 0)
                        or not current.get("header_detected")
                    )
                )
                if stronger:
                    if current is None:
                        source_tables.append(structure_table)
                    else:
                        source_tables[source_tables.index(current)] = structure_table
                    data_bottom = max(
                        float(data_bottom or 0.0), float(structure_table["source_bbox"][3])
                    )
                    methods_attempted.append("rapid_table_structure_fallback")

    for table in source_tables:
        _split_repeated_quantity_measure(table)

    raw_summaries = (
        positioned_summary_candidates
        if positioned_summary_candidates is not None and source_tables
        else _find_summary_candidates(page, visual_lines) if source_tables else []
    )
    if getattr(page, "ocr_word_count", 0) and raw_summaries:
        data_bottom = _detach_ocr_summary_candidates_from_item_rows(
            page, source_tables, raw_summaries, data_bottom,
        )
    summary_items = _public_summary_items(raw_summaries, data_bottom)
    metadata_items = _extract_document_metadata(page, source_tables, raw_summaries, visual_lines)

    # Normalize roles for every source-derived table. These are region types,
    # not vendor names or predefined invoice-column schemas.
    if timing_metrics is not None:
        timing_metrics["table_reconstruction_seconds"] = time.perf_counter() - reconstruction_started
    validation_started = time.perf_counter()
    for table in source_tables:
        table["is_source_table"] = True
        if table.get("layout_type") == "document_form":
            table["table_role"] = "document_header"
        else:
            table.setdefault("table_role", "line_items")
            table.setdefault("layout_type", "item_table")
        if getattr(page, "ocr_word_count", 0):
            _annotate_ocr_table_review(page, table)
            _recover_missing_ocr_text_line_from_geometry(page, table)
            _normalize_confusable_numeric_ocr_cells(page, table)
            _annotate_colored_stamp_overlaps(page, table)
            _clear_non_numeric_text_from_ocr_numeric_lanes(table)
            _recover_missing_numeric_cells_from_repeated_table_arithmetic(table)
            _attach_parent_item_context(page, table, visual_lines)
            _attach_ocr_cell_evidence(page, table)
        else:
            _attach_pre_row_context(page, table, visual_lines)
    if timing_metrics is not None:
        timing_metrics["validation_seconds"] = time.perf_counter() - validation_started

    if source_tables and summary_items:
        summary_target = next(
            (table for table in source_tables if table.get("table_role") == "line_items"),
            None,
        )
        if summary_target is not None:
            summary_target["summary_items"] = summary_items

    # Build one logical metadata table per page. Keep metadata attached to one
    # source table as a compatibility bridge for older frontend callers, rather
    # than duplicating it on every table.
    metadata_table = _build_document_metadata_table(metadata_items)
    metadata_owner = next(
        (table for table in source_tables if table.get("table_role") == "line_items"),
        source_tables[0] if source_tables else None,
    )
    if metadata_owner is not None:
        metadata_owner["metadata_items"] = metadata_items

    # ``tables`` remains backward-compatible: it contains only source-derived
    # table regions. The separate metadata table is returned under its own key
    # and is never mixed into native table counts or item-table indexing.
    for source_index, table in enumerate(source_tables, start=1):
        table["table_index"] = source_index

    status = "table_found" if source_tables else "no_table_detected"
    if not source_tables and methods_attempted and len(methods_failed) == len(methods_attempted):
        status = "detection_error"

    # For a text page with metadata but no native table, expose the logical
    # metadata block in `tables` for the existing frontend renderer. It is
    # explicitly marked non-source and does not affect source_table_count/status.
    # When native tables exist, keep the `tables` list strictly source-first and
    # use the same metadata records as the separate page-level block.
    response_tables = source_tables if source_tables else ([metadata_table] if metadata_table is not None else [])

    return {
        "status": status,
        "tables": response_tables,
        "source_table_count": len(source_tables),
        "metadata_items": metadata_items,
        "document_metadata_table": metadata_table,
        "summary_items": summary_items,
        "methods_attempted": methods_attempted,
        "methods_failed": methods_failed,
    }
