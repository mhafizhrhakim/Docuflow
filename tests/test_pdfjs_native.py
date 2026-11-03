"""Regression coverage for the fast PDF.js source-grid path."""
from pathlib import Path
import shutil

import pytest

from backend.services.pdfjs_native import extract_grid_table, inspect_with_pdfjs
from backend.services.document_parser import inspect_pdf

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "real_documents" / "G031-0018-2026-M HFJ-260436177.pdf"
RQN_SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "real_documents" / "RQN GI30-26V-0057-1 DD-Spare Part Pilot Operated Safety Valve.pdf"
HFJ_TWO_SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "real_documents" / "HFJ-260436177-2.pdf"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js runtime is unavailable")
@pytest.mark.skipif(not SAMPLE.exists(), reason="real regression PDF is not present")
def test_pdfjs_grid_path_recovers_source_item_rows_and_numeric_lanes():
    inspected = inspect_with_pdfjs(SAMPLE.read_bytes())
    assert inspected is not None
    pages, _elapsed = inspected

    expected = {
        2: (12, "3", "14", ("20", "MTR", "2.82", "56.40")),
        15: (13, "173", "185", ("6", "SET", "12.26", "73.56")),
        16: (11, "186", "196", ("1", "ST", "146.36", "146.36")),
    }
    for page_number, (row_count, first_id, last_id, values) in expected.items():
        table = extract_grid_table(pages[page_number - 1])
        assert table is not None
        assert table["column_count"] == 10
        assert table["headers"] == [
            "#", "Part Type", "Part Number", "Supplier Part No.",
            "Description", "UoM", "Qty", "Unit Cost", "Disc %", "Total Cost",
        ]
        assert table["row_count"] == row_count
        assert table["rows"][0][0] == first_id
        assert table["rows"][-1][0] == last_id
        first_row = table["rows"][0]
        assert (first_row[6], first_row[5], first_row[7], first_row[9]) == values
        assert all(len(row) == 10 for row in table["rows"])


@pytest.mark.skipif(not SAMPLE.exists(), reason="real regression PDF is not present")
def test_production_parser_uses_fast_grid_for_expensive_native_page():
    """The production route must bypass costly table finders when grid evidence is strong."""
    result = inspect_pdf(SAMPLE.read_bytes(), run_ocr=True)
    page = result["pages"][14]

    assert page["table_extraction_status"] == "table_found"
    assert page["ocr_applied"] is False
    assert page["table_detection_methods"] == ["pdfjs_source_grid"]
    assert page["source_table_count"] == 1

    table = next(
        item for item in page["tables"]
        if item.get("is_source_table") is not False
    )
    assert table["headers"] == [
        "#", "Part Type", "Part Number", "Supplier Part No.",
        "Description", "UoM", "Qty", "Unit Cost", "Disc %", "Total Cost",
    ]
    assert table["column_count"] == 10
    assert table["row_count"] == 13
    assert table["rows"][0][0] == "173"
    assert (table["rows"][0][6], table["rows"][0][5], table["rows"][0][7], table["rows"][0][9]) == (
        "6", "SET", "12.26", "73.56",
    )
    assert table["rows"][-1][0] == "185"
    assert all(len(row) == 10 for row in table["rows"])


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js runtime is unavailable")
@pytest.mark.skipif(not SAMPLE.exists(), reason="real regression PDF is not present")
def test_pdfjs_grid_path_rejects_document_header_as_line_items():
    inspected = inspect_with_pdfjs(SAMPLE.read_bytes())
    assert inspected is not None
    pages, _elapsed = inspected
    assert extract_grid_table(pages[0]) is None


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js runtime is unavailable")
@pytest.mark.skipif(not HFJ_TWO_SAMPLE.exists(), reason="real source PDF is not present")
def test_pdfjs_grid_rejects_page_metadata_band_from_hfj_continuation_page():
    inspected = inspect_with_pdfjs(HFJ_TWO_SAMPLE.read_bytes())
    assert inspected is not None
    pages, _elapsed = inspected
    # This page's real item table has native text, but the fast-grid candidate
    # points at invoice/page metadata above it, so the native table extractor
    # must handle the page instead.
    assert extract_grid_table(pages[3]) is None


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js runtime is unavailable")
@pytest.mark.skipif(not RQN_SAMPLE.exists(), reason="real source PDF is not present")
def test_pdfjs_grid_defers_grouped_headers_to_native_table_reconstruction():
    inspected = inspect_with_pdfjs(RQN_SAMPLE.read_bytes())
    assert inspected is not None
    pages, _elapsed = inspected

    # PDF.js fast-grid only emits flat headers. The source has a merged Balance
    # heading over New/Used, so native extraction must preserve that hierarchy.
    assert extract_grid_table(pages[2]) is None
