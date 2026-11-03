"""Regressions for geometry-based quantity and unit column assignment."""
from pathlib import Path

import pymupdf
import pytest

from backend.services.ocr_adapter import run_ocr_for_page
from backend.services.table_extractor import (
    _assign_run_to_geometry_column,
    _column_boundaries_from_header,
    _split_run_at_column_boundaries,
    extract_tables_from_page,
)


SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples" / "real_documents"
HANIL_SAMPLE = SAMPLE_DIR / "HFJ-260436177-2.pdf"


def _header_index(headers: list[str], label: str) -> int:
    normalized = label.casefold()
    return next(i for i, value in enumerate(headers) if value.strip().casefold() == normalized)


def test_tight_adjacent_word_boxes_follow_header_derived_boundary() -> None:
    header_runs = [
        {"x0": 363.03, "x1": 382.53},
        {"x0": 391.76, "x1": 411.26},
    ]
    boundaries = _column_boundaries_from_header(header_runs)
    # The quantity starts closer to the UNIT header's left edge, but its bbox
    # remains inside the Q'TY lane. Start-only assignment put it in UNIT.
    for value, unit in [("6", "PCS"), ("1", "LGH"), ("1,250.5", "PACK")]:
        words = [
            {"text": value, "x0": 377.65, "x1": 382.53, "y0": 100.0, "y1": 110.0},
            {"text": unit, "x0": 394.19, "x1": 408.82, "y0": 100.0, "y1": 110.0},
        ]
        run = {
            "text": f"{value} {unit}", "x0": words[0]["x0"],
            "x1": words[-1]["x1"], "y0": 100.0, "y1": 110.0,
            "words": words,
        }
        pieces = _split_run_at_column_boundaries(run, boundaries)
        if len(pieces) == 1:
            piece, _ = pieces[0]
            assert _assign_run_to_geometry_column(piece, [363.03, 391.76], boundaries) == 0
        else:
            assert [(piece["text"], column) for piece, column in pieces] == [
                (value, 0), (unit, 1),
            ]


@pytest.mark.skipif(not HANIL_SAMPLE.exists(), reason="local source PDF is not present")
def test_hanil_digital_source_keeps_quantity_and_unit_separate() -> None:
    document = pymupdf.open(HANIL_SAMPLE)
    try:
        result = extract_tables_from_page(document[2])
    finally:
        document.close()

    table = result["tables"][0]
    qty = _header_index(table["headers"], "Q'TY")
    unit = _header_index(table["headers"], "UNIT")
    assert len(table["rows"]) == 15
    item_11 = next(row for row in table["rows"] if row[0].strip().rstrip(".") == "11")
    assert "FLANGE STAINLESS STEEL WELDING, SLIP-ON FLAT FACE" in item_11[1]
    item_12 = next(row for row in table["rows"] if row[0].strip().rstrip(".") == "12")
    assert "PIPE STAINLESS STEEL SUS-304" in item_12[1]
    assert (item_11[qty], item_11[unit]) == ("6", "PCS")
    assert (item_12[qty], item_12[unit]) == ("2", "LGH")


@pytest.mark.skipif(not HANIL_SAMPLE.exists(), reason="local source PDF is not present")
def test_hanil_scanned_source_keeps_rows_three_to_six_aligned() -> None:
    document = pymupdf.open(HANIL_SAMPLE)
    try:
        ocr_page = run_ocr_for_page(document[14], dpi=150)
        result = extract_tables_from_page(ocr_page)
    finally:
        document.close()

    table = result["tables"][0]
    qty = _header_index(table["headers"], "Q'TY")
    unit = _header_index(table["headers"], "UNIT")
    description = _header_index(table["headers"], "DESCRIPTION")
    rows = {row[0].rstrip("."): row for row in table["rows"]}
    assert len(rows) == 15
    assert set(rows) == {str(item) for item in range(1, 16)}
    assert (rows["3"][qty], rows["3"][unit]) == ("20", "MTR")
    assert (rows["4"][qty], rows["4"][unit]) == ("1", "SET")
    assert (rows["5"][qty], rows["5"][unit]) == ("1", "SET")
    assert (rows["6"][qty], rows["6"][unit]) == ("2", "PRS")
    assert "OFFER = GREENPOWER" in rows["4"][description]


@pytest.mark.skipif(not HANIL_SAMPLE.exists(), reason="local source PDF is not present")
def test_hanil_scanned_page_19_splits_tightly_spaced_quantity_unit_lanes() -> None:
    document = pymupdf.open(HANIL_SAMPLE)
    try:
        ocr_page = run_ocr_for_page(document[18], dpi=150)
        result = extract_tables_from_page(ocr_page)
    finally:
        document.close()

    table = result["tables"][0]
    qty = _header_index(table["headers"], "Q'TY")
    unit = _header_index(table["headers"], "UNIT")
    description = _header_index(table["headers"], "DESCRIPTION")
    code = _header_index(table["headers"], "CODE")
    rows = {row[0].rstrip("."): row for row in table["rows"]}
    # The rendered source page contains items 93–116. The previous expected
    # range stopped at 107 and silently treated nine visible source rows as if
    # they belonged to a later page.
    expected = {
        "93": ("1", "SET"), "94": ("1", "SET"), "95": ("1", "SET"),
        "96": ("2", "PCS"), "97": ("1", "PCS"), "98": ("2", "SHT"),
        "99": ("2", "SHT"), "100": ("4", "SHT"), "101": ("4", "SHT"),
        "102": ("4", "SHT"), "103": ("3", "SHT"), "104": ("4", "SHT"),
        "105": ("3", "SHT"), "106": ("3", "PCS"), "107": ("1", "PCS"),
        "108": ("2", "PCS"), "109": ("2", "PCS"), "110": ("2", "PCS"),
        "111": ("2", "PCS"), "112": ("1", "PCS"), "113": ("1", "PCS"),
        "114": ("1", "PCS"), "115": ("2", "PCS"), "116": ("2", "PCS"),
    }
    assert table["headers"] == ["NO.", "DESCRIPTION", "CODE", "Q'TY", "UNIT"]
    assert len(rows) == 24
    assert set(rows) == {str(number) for number in range(93, 117)}
    assert {item: (rows[item][qty], rows[item][unit]) for item in expected} == expected

    # Long descriptions span toward the CODE lane. Use the right-hand body lane
    # boundary, preserve numeric description continuations, and keep ambiguous
    # overlapping OCR boxes explicitly marked for review.
    assert rows["110"][code] == "232602"
    assert "ORDERED FROM CATALOG IMPA: 232602, IMPA NO: 232602" in rows["110"][description]
    assert rows["111"][code] == "232606"
    assert "ORDERED FROM CATALOG IMPA: 232606, IMPA NO: 232606" in rows["111"][description]

    row_114_index = next(i for i, row in enumerate(table["rows"]) if row[0].rstrip(".") == "114")
    assert (rows["114"][qty], rows["114"][unit]) == ("1", "PCS")
    assert table["rows_need_review"][row_114_index] is True
    assert "row_alignment_uncertain" in table["row_review_reasons"][row_114_index]

    row_110_index = next(i for i, row in enumerate(table["rows"]) if row[0].rstrip(".") == "110")
    assert table["rows_need_review"][row_110_index] is True
    assert "row_alignment_uncertain" in table["row_review_reasons"][row_110_index]
