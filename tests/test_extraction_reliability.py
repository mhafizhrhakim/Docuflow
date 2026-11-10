"""Source-backed regression coverage for confirmed extraction failures."""
from pathlib import Path
import hashlib
import json
import re
from typing import Any

import pymupdf
import pytest

from backend.services.ocr_adapter import run_ocr_for_page
from backend.services.document_parser import inspect_pdf
from backend.services.table_extractor import _extract_from_native_candidates, extract_tables_from_page


SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples" / "real_documents"


def _single_page_pdf(source: Path, page_index: int) -> bytes:
    original = pymupdf.open(source)
    result = pymupdf.open()
    try:
        result.insert_pdf(original, from_page=page_index, to_page=page_index)
        return result.tobytes()
    finally:
        result.close()
        original.close()


@pytest.mark.skipif(not (SAMPLE_DIR / "G031-0018-2026-M.pdf").exists(), reason="local source PDF is not present")
def test_native_qty_and_unit_share_a_source_cell_but_are_returned_separately() -> None:
    document = pymupdf.open(SAMPLE_DIR / "G031-0018-2026-M.pdf")
    try:
        result = extract_tables_from_page(document[1])
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert table["headers"] == ["L.no", "Qty", "Unit", "Description", "Unit price", "Total price"]
    rows = {row[0]: row for row in table["rows"]}
    qty_index = table["headers"].index("Qty")
    unit_index = table["headers"].index("Unit")
    assert all(rows[item][qty_index].replace(".", "", 1).isdigit() for item in rows)
    assert [(rows[item][qty_index], rows[item][unit_index]) for item in ("8", "9", "10", "11")] == [
        ("6", "PCE"), ("6", "PCE"), ("6", "PCE"), ("6", "PCE"),
    ]
    assert rows["4"][unit_index] == rows["5"][unit_index] == "SET"
    assert table["rows_need_review"][2:4] == [True, True]


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
def test_digital_hfj_page_2_keeps_item_number_and_description_in_separate_columns() -> None:
    document = pymupdf.open(SAMPLE_DIR / "HFJ-260436177-2.pdf")
    try:
        result = extract_tables_from_page(document[1])
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert "text" not in result["methods_attempted"]
    assert table["headers"] == ["NO.", "DESCRIPTION", "Q'TY", "UNIT", "U/PRICE", "AMOUNT"]
    first = table["rows"][0]
    assert first[0] == "1"
    assert "WIPING RAGS, ORDERED FROM CATALOG IMPA: 232907, IMPA" in first[1]
    assert "NO: 232907" in first[1]
    assert "OFFER = A GRADE, PEICE PER KGS" in first[1]
    assert first[2:] == ["250", "KGS", "1.15", "287.50"]


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
def test_hfj_page_2_production_route_uses_native_table_without_ocr():
    source = SAMPLE_DIR / "HFJ-260436177-2.pdf"
    result = inspect_pdf(_single_page_pdf(source, 1), run_ocr=True)
    page = result["pages"][0]
    table = next(item for item in page["tables"] if item.get("table_role") == "line_items")
    assert page["source_candidate"] == "mixed_candidate"
    assert page["ocr_applied"] is False
    assert table["headers"] == ["NO.", "DESCRIPTION", "Q'TY", "UNIT", "U/PRICE", "AMOUNT"]
    assert table["row_count"] == 10
    assert table["rows"][0][2:] == ["250", "KGS", "1.15", "287.50"]


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
def test_hfj_summary_currency_stays_in_amount_column_not_account_name() -> None:
    document = pymupdf.open(SAMPLE_DIR / "HFJ-260436177-2.pdf")
    try:
        result = extract_tables_from_page(document[0])
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert table["headers"] == ["INVOICE NO", "P.O.NO", "ACC.NAME", "AMOUNT"]
    row = table["rows"][0]
    assert row == ["HFJ-260436177-2", "G031-0018-2026-M", "", "USD 7,630.95"]
    assert row[3] == "USD 7,630.95"
    summary = table["summary_items"][0]
    assert summary["label"] == "TOTAL AMOUNT (USD)"
    assert summary["value"] == "7,630.95"
    assert len(summary["source_bbox"]) == 4


@pytest.mark.skipif(not (SAMPLE_DIR / "G031-0034-2026-E RFQ_G031-0034-2026-E_B(2).pdf").exists(), reason="local source PDF is not present")
def test_rfq_page_2_combines_headerless_continuation_into_one_source_table() -> None:
    document = pymupdf.open(SAMPLE_DIR / "G031-0034-2026-E RFQ_G031-0034-2026-E_B(2).pdf")
    try:
        result = extract_tables_from_page(document[1])
    finally:
        document.close()

    tables = [table for table in result["tables"] if table.get("table_role") == "line_items"]
    assert len(tables) == 1
    table = tables[0]
    assert table["headers"] == ["L.no", "Qty", "UOM", "Description"]
    assert table["rows"] == [
        ["1", "2", "PCE", "Flame Detection Relay"],
        ["2", "2", "PCE", "Flame Detection Relay"],
    ]


@pytest.mark.skipif(not (SAMPLE_DIR / "KIUCHI 0014.pdf").exists(), reason="local source PDF is not present")
def test_kiuchi_headerless_ocr_rows_recover_repeated_numeric_lanes_and_parent_context() -> None:
    source = SAMPLE_DIR / "KIUCHI 0014.pdf"
    document = pymupdf.open(source)
    try:
        page = run_ocr_for_page(
            document[0], dpi=150, cache_key=hashlib.sha256(source.read_bytes()).hexdigest(),
            page_number=1,
        )
        result = extract_tables_from_page(page)
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert table["row_recovery_strategy"] == "ocr_repeated_numeric_lanes"
    assert table["row_count"] == 2
    assert table["rows"] == [
        ["Main Body Soft Parts Set", "2 sets", "320,000", "640,000", "84819000"],
        ["Pilot Valve Soft Parts Set", "2 sets", "160,000", "320,000", "84819000"],
    ]
    assert table["headers"] == ["", "", "", "", "HS code"]
    assert table["rows_need_review"] == [True, True]
    assert table["review_required"] is True
    assert table["parent_item_context"]["identifier"] == "1"
    assert "Soft Parts For Cargo Tank Safety Valves" in table["parent_item_context"]["description"]


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
def test_hfj_mixed_page_4_native_route_recovers_rows_26_through_43() -> None:
    document = pymupdf.open(SAMPLE_DIR / "HFJ-260436177-2.pdf")
    try:
        result = extract_tables_from_page(document[3])
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert table["headers"] == ["NO.", "DESCRIPTION", "Q'TY", "UNIT", "U/PRICE", "AMOUNT"]
    assert [int(row[0]) for row in table["rows"]] == list(range(26, 44))
    assert table["rows"][0][2:] == ["24", "PCS", "0.92", "22.08"]
    assert table["rows"][-1][2:] == ["24", "SET", "0.07", "1.68"]


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
def test_hfj_page_4_production_route_rejects_metadata_band_and_keeps_native_rows() -> None:
    result = inspect_pdf(
        _single_page_pdf(SAMPLE_DIR / "HFJ-260436177-2.pdf", 3), run_ocr=True,
    )
    page = result["pages"][0]
    table = next(item for item in page["tables"] if item.get("table_role") == "line_items")
    assert page["source_candidate"] == "mixed_candidate"
    assert page["ocr_applied"] is False
    assert page["table_detection_methods"] != ["pdfjs_source_grid"]
    assert table["row_count"] == 18
    assert [int(row[0]) for row in table["rows"]] == list(range(26, 44))


@pytest.mark.skipif(not (SAMPLE_DIR / "RQN GI30-26V-0057-1 DD-Spare Part Pilot Operated Safety Valve.pdf").exists(), reason="local source PDF is not present")
def test_rqn_page_3_production_route_classifies_mixed_and_keeps_actual_item_table() -> None:
    source = SAMPLE_DIR / "RQN GI30-26V-0057-1 DD-Spare Part Pilot Operated Safety Valve.pdf"
    result = inspect_pdf(_single_page_pdf(source, 2), run_ocr=True)
    page = result["pages"][0]
    table = next(item for item in page["tables"] if item.get("table_role") == "line_items")
    assert page["source_candidate"] == "mixed_candidate"
    assert page["has_extractable_text"] is True
    assert page["ocr_applied"] is False
    assert table["headers"] == [
        "Item No.", "Name of part(s)", "Drawing no.", "Part no.",
        "Working qty", "Balance / New", "Balance / Used", "Req’d qty", "Remarks",
    ]
    assert table["header_rows"][0][5:7] == ["Balance", "Balance"]
    assert table["header_rows"][1][5:7] == ["New", "Used"]
    balance = next(cell for cell in table["header_cells"] if cell["text"] == "Balance")
    assert balance["column"] == 5 and balance["colspan"] == 2
    assert table["row_count"] == 16
    assert [row[0] for row in table["rows"]] == [str(number) for number in range(1, 17)]


@pytest.mark.skipif(not (SAMPLE_DIR / "G031-0034-2026-E RFQ_G031-0034-2026-E_B(2).pdf").exists(), reason="local source PDF is not present")
def test_rfq_page_2_production_route_merges_headerless_continuation_rows() -> None:
    source = SAMPLE_DIR / "G031-0034-2026-E RFQ_G031-0034-2026-E_B(2).pdf"
    result = inspect_pdf(_single_page_pdf(source, 1), run_ocr=True)
    page = result["pages"][0]
    tables = [table for table in page["tables"] if table.get("table_role") == "line_items"]
    assert page["ocr_applied"] is False
    assert len(tables) == 1
    assert tables[0]["rows"] == [
        ["1", "2", "PCE", "Flame Detection Relay"],
        ["2", "2", "PCE", "Flame Detection Relay"],
    ]


@pytest.mark.skipif(not (SAMPLE_DIR / "G031-0034-2026-E quot Atlas 162821  Gas Quila.pdf").exists(), reason="local source PDF is not present")
def test_quote_production_route_selects_item_table_not_document_metadata() -> None:
    source = SAMPLE_DIR / "G031-0034-2026-E quot Atlas 162821  Gas Quila.pdf"
    result = inspect_pdf(_single_page_pdf(source, 0), run_ocr=True)
    page = result["pages"][0]
    table = next(item for item in page["tables"] if item.get("table_role") == "line_items")
    assert page["ocr_applied"] is False
    assert table["headers"] == ["Item No.", "Description", "Qty.", "Unit", "Unit Price", "Amount"]
    assert table["row_count"] == 6
    assert table["rows"][0][0] == "57K4012"
    assert table["rows"][0][2:] == ["2", "PC", "5.210,00", "10.420,00"]


@pytest.mark.skipif(not (SAMPLE_DIR / "ASIN130370.pdf").exists(), reason="local source PDF is not present")
def test_asin_main_table_and_header_only_table_are_detected_on_pages_1_and_2() -> None:
    source = SAMPLE_DIR / "ASIN130370.pdf"
    first = inspect_pdf(_single_page_pdf(source, 0), run_ocr=False)["pages"][0]
    second = inspect_pdf(_single_page_pdf(source, 1), run_ocr=False)["pages"][0]
    item_table = next(item for item in first["tables"] if item.get("table_role") == "line_items")
    assert item_table["row_count"] == 6
    assert item_table["headers"] == ["Item No.", "Description", "Qty.", "Unit", "Unit Price", "Amount"]
    context_text = " ".join(item.get("text", "") for item in item_table.get("context_rows", []))
    assert "SPARE PARTS F. INCINERATOR" in context_text
    header_table = next(item for item in second["tables"] if item.get("is_source_table"))
    assert second["source_table_count"] == 1
    assert second["table_extraction_status"] == "table_found"
    assert header_table["headers"] == ["Item No.", "Description", "Qty. Unit", "Unit Price", "Amount"]
    assert header_table["header_detected"] is True
    assert header_table["row_count"] == 0
    assert header_table["rows"] == []
    assert header_table["column_count"] == 5
    assert len(second["metadata_items"]) > 0
    assert not any(
        item["label"] == "Item No." and item["value"] == "Description"
        for item in second["metadata_items"]
    )


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
@pytest.mark.parametrize(
    ("page_number", "expected_ids", "expected_pairs"),
    [
        (18, [68, *range(70, 93)], {"68": ("1", "PCS"), "70": ("10", "PCS"), "71": ("10", "EA")}),
        (20, [*range(117, 122), *range(123, 128), *range(129, 145)], {"117": ("2", "PCS"), "121": ("2", "PCS"), "129": ("3", "ROLL")}),
        (21, list(range(145, 173)), {"145": ("20", "EA"), "146": ("2", "PCS"), "154": ("10", "PCS"), "172": ("12", "PCS")}),
        (23, [196], {"196": ("10", "TIN")}),
    ],
)
def test_scanned_continuation_pages_keep_table_header_and_column_lanes(
    page_number: int,
    expected_ids: list[int],
    expected_pairs: dict[str, tuple[str, str]],
) -> None:
    document = pymupdf.open(SAMPLE_DIR / "HFJ-260436177-2.pdf")
    try:
        page = run_ocr_for_page(document[page_number - 1], dpi=150)
        result = extract_tables_from_page(page)
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert table["headers"] == ["NO.", "DESCRIPTION", "CODE", "Q'TY", "UNIT"]
    assert table["row_count"] == len(expected_ids)
    rows = {row[0].strip().rstrip("."): row for row in table["rows"]}
    assert set(rows) == {str(item) for item in expected_ids}
    quantity = table["headers"].index("Q'TY")
    unit = table["headers"].index("UNIT")
    assert {item: (rows[item][quantity], rows[item][unit]) for item in expected_pairs} == expected_pairs


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
def test_hfj_scan_page_22_recovers_source_rows_and_quantity_unit_lanes() -> None:
    document = pymupdf.open(SAMPLE_DIR / "HFJ-260436177-2.pdf")
    try:
        page = run_ocr_for_page(document[21], dpi=150)
        result = extract_tables_from_page(page)
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert table["headers"] == ["NO.", "DESCRIPTION", "CODE", "Q'TY", "UNIT"]
    expected = {
        "173": ("4", "SET"), "174": ("1", "PCS"), "175": ("1", "SET"),
        "176": ("4", "PCS"), "177": ("1", "PCS"), "178": ("1", "SET"),
        "179": ("1", "SET"), "180": ("3", "PRS"), "181": ("6", "PCS"),
        "182": ("2", "BTL"), "183": ("3", "SET"), "184": ("1", "MTR"),
        "185": ("1", "SHT"), "186": ("1", "SHT"), "188": ("10", "EA"),
        "189": ("4", "LGH"), "190": ("4", "LGH"), "191": ("3", "LGH"),
        "192": ("2", "LGH"), "193": ("13", "PRS"), "194": ("2", "PCS"),
        "195": ("2", "PCS"),
    }
    id_index = 0
    description_index = 1
    quantity_index = table["headers"].index("Q'TY")
    unit_index = table["headers"].index("UNIT")
    rows = {row[id_index].strip().rstrip("."): row for row in table["rows"]}
    assert set(rows) == set(expected)
    assert {item: (row[quantity_index], row[unit_index]) for item, row in rows.items()} == expected
    assert len(table["rows"]) == 22
    assert "CORDLESS DRILL DRIVERS" in rows["174"][description_index]
    assert "CORDLESS IMPACT WRENCHES" in rows["175"][description_index]
    assert "L- OFFER = DJD 282" in rows["173"][description_index]
    assert "L OFFER = KENPO" in rows["175"][description_index]


@pytest.mark.skipif(not (SAMPLE_DIR / "HFJ-260436177-2.pdf").exists(), reason="local source PDF is not present")
def test_hfj_scan_page_17_uses_table_header_instead_of_document_metadata_band() -> None:
    source = SAMPLE_DIR / "HFJ-260436177-2.pdf"
    source_bytes = source.read_bytes()
    document = pymupdf.open(source)
    try:
        page = run_ocr_for_page(
            document[16], dpi=150, cache_key=hashlib.sha256(source_bytes).hexdigest(),
            page_number=17,
        )
        result = extract_tables_from_page(page)
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert table["headers"] == ["NO.", "DESCRIPTION", "CODE", "Q'TY", "UNIT"]
    assert table["row_count"] == 26
    assert [row[0].strip().rstrip(".") for row in table["rows"]] == [str(number) for number in range(42, 68)]
    assert table["rows"][0][2:] == ["692545", "24", "SET"]
    assert table["rows"][-1][2:] == ["670881", "4", "SHT"]


@pytest.mark.skipif(not (SAMPLE_DIR / "G031-0018-2026-M PO_G031-0018-2026-M.pdf").exists(), reason="local source PDF is not present")
def test_native_po_page_2_merges_supported_fragments_and_recovers_first_quantity() -> None:
    document = pymupdf.open(SAMPLE_DIR / "G031-0018-2026-M PO_G031-0018-2026-M.pdf")
    try:
        result = extract_tables_from_page(document[1])
    finally:
        document.close()

    tables = [table for table in result["tables"] if table.get("table_role") == "line_items"]
    assert len(tables) == 1
    table = tables[0]
    assert table["row_count"] == 9
    assert table["merged_fragment_count"] == 9
    assert [row[0] for row in table["rows"]] == [str(number) for number in range(1, 10)]
    qty = table["headers"].index("Qty")
    uom = table["headers"].index("UOM")
    description = table["headers"].index("Description")
    assert (table["rows"][0][qty], table["rows"][0][uom]) == ("250", "KGM")
    assert table["rows"][0][description] == "Wiping Rags"
    assert table["rows"][1][description] == "HOIST CHAIN SPUR-GEAR 1/2TON, 2.5MTR"
    assert table["rows"][8][description] == "FLANGE STAINLESS STEEL WELDING, SLIP-ON FLAT FACE 10KG-100MM"


def test_repeated_native_headers_and_section_rows_are_context_not_item_rows() -> None:
    # These two source item rows and the repeated header/section shape are from
    # G031 HFJ pages 15–16. The expensive source page is not reprocessed here.
    header = ["#", "Part Type", "Part Number", "Description", "UoM", "Qty"]
    candidate = {
        "bbox": (10.0, 10.0, 400.0, 180.0),
        "column_count": 6,
        "strategy": "lines",
        "rows": [
            header,
            ["173", "", "330620", "HEAD TORCH LED SAFETY, WOLF HT-400", "SET", "6"],
            ["Equipment Section Name: PART INFORMATION Serial #: 590906", "", "", "", "", ""],
            header,
            ["174", "", "590906", "CORDLESS DRILL DRIVERS", "PCE", "1"],
        ],
        "row_bboxes": [
            (10, 10, 400, 20), (10, 20, 400, 35), (10, 35, 400, 45),
            (10, 45, 400, 55), (10, 55, 400, 70),
        ],
        "cell_bboxes": [
            [(10,10,40,20),(40,10,70,20),(70,10,130,20),(130,10,300,20),(300,10,350,20),(350,10,400,20)],
            [(10,20,40,35),(40,20,70,35),(70,20,130,35),(130,20,300,35),(300,20,350,35),(350,20,400,35)],
            [(10,35,400,45),None,None,None,None,None],
            [(10,45,40,55),(40,45,70,55),(70,45,130,55),(130,45,300,55),(300,45,350,55),(350,45,400,55)],
            [(10,55,40,70),(40,55,70,70),(70,55,130,70),(130,55,300,70),(300,55,350,70),(350,55,400,70)],
        ],
    }
    tables, _ = _extract_from_native_candidates([candidate])
    table = next(item for item in tables if item.get("table_role") == "line_items")
    assert [row[0] for row in table["rows"]] == ["173", "174"]
    assert len([row for row in table["context_rows"] if row["type"] == "repeated_header"]) == 1
    assert any(row["type"] == "possible_section_heading" and "PART INFORMATION" in row["text"] for row in table["context_rows"])


@pytest.mark.parametrize(
    ("page_index", "expected_ids", "repeated_headers", "expected_pairs"),
    [
        (14, list(range(173, 186)), 12, [
            ("6", "SET"), ("1", "PCE"), ("1", "SET"), ("4", "PCE"),
            ("1", "PCE"), ("1", "SET"), ("1", "SET"), ("3", "PR"),
            ("6", "PCE"), ("2", "BO"), ("3", "SET"), ("1", "MTR"), ("1", "ST"),
        ]),
        (15, list(range(186, 197)), 10, [
            ("1", "ST"), ("1", "ST"), ("2", "PCE"), ("4", "LN"),
            ("4", "LN"), ("3", "LN"), ("2", "LN"), ("13", "PR"),
            ("2", "PCE"), ("2", "PCE"), ("10", "TN"),
        ]),
    ],
)
def test_g031_source_candidates_keep_items_and_repeated_headers_as_context(
    page_index: int,
    expected_ids: list[int],
    repeated_headers: int,
    expected_pairs: list[tuple[str, str]],
) -> None:
    # Reuse exact cached production rows for these pathological pages; p15
    # reported 699,846 image entries and took about 158 s to inspect natively.
    benchmark_path = Path(__file__).resolve().parents[1] / "benchmark-results" / "benchmark_18_pdfs.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    document = next(item for item in benchmark["documents"] if item["filename"] == "G031-0018-2026-M HFJ-260436177.pdf")
    page = document["pages"][page_index]
    source = page["tables"][0]
    assert source["row_count"] == len(expected_ids) + repeated_headers

    header_boxes = [tuple(cell["source_bbox"]) for cell in source["header_cells"]]
    header_bbox = tuple(source["header_source_bbox"])
    rows = [source["headers"], *source["rows"]]
    row_bboxes = [header_bbox, *[tuple(box) for box in source["row_source_bboxes"]]]
    cell_bboxes = [header_boxes]
    for row_bbox in row_bboxes[1:]:
        cell_bboxes.append([
            (header_box[0], row_bbox[1], header_box[2], row_bbox[3])
            for header_box in header_boxes
        ])
    candidate = {
        "bbox": tuple(source["source_bbox"]),
        "column_count": source["column_count"],
        "strategy": "lines",
        "rows": rows,
        "row_bboxes": row_bboxes,
        "cell_bboxes": cell_bboxes,
    }

    tables, _ = _extract_from_native_candidates([candidate])
    table = next(item for item in tables if item.get("table_role") == "line_items")
    assert table["row_count"] == len(expected_ids)
    assert [row[0] for row in table["rows"]] == [str(number) for number in expected_ids]
    quantity = table["headers"].index("Qty")
    uom = table["headers"].index("UoM")
    assert [(row[quantity], row[uom]) for row in table["rows"]] == expected_pairs
    assert len([row for row in table["context_rows"] if row["type"] == "repeated_header"]) == repeated_headers


@pytest.mark.skipif(not (SAMPLE_DIR / "VIF2503-158I.pdf").exists(), reason="local source PDF is not present")
def test_vif_subitems_retain_source_visible_parent_context() -> None:
    document = pymupdf.open(SAMPLE_DIR / "VIF2503-158I.pdf")
    try:
        page = run_ocr_for_page(document[0], dpi=150)
        result = extract_tables_from_page(page)
    finally:
        document.close()

    table = next(item for item in result["tables"] if item.get("table_role") == "line_items")
    assert [row[0] for row in table["rows"]] == ["a", "b"]
    parent = table["parent_item_context"]
    # The source image prints 1, but the production OCR word output misses it.
    # Keep the relationship and label while exposing the unread identifier.
    assert parent["identifier"] is None
    assert parent["description"] == "Cargo Tank Safety Relief Valve Spares"
    assert parent["child_item_ids"] == ["a", "b"]
    assert parent["identifier_needs_review"] is True
    assert [(link["child_identifier"], link["parent_identifier"]) for link in table["row_parent_links"]] == [("a", None), ("b", None)]
    assert any(issue["code"] == "parent_identifier_not_read" for issue in table["review_issues"])
    assert table["rows"][0][1:] == ["2 sets", "Main valve soft parts sets", "320,000", "640,000"]
    assert table["rows"][1][1:] == ["2 sets", "Pilot valve soft parts sets", "160,000", "320,000"]


ASTERION_TESTER_PDF = Path.home() / "Downloads" / "Asterion_Ship_Suppliers_Invoice_Test_4P_Final.pdf"


@pytest.mark.skipif(not ASTERION_TESTER_PDF.exists(), reason="local Asterion tester PDF is not present in Downloads")
def test_asterion_digital_and_scanned_pages_keep_line_item_columns_aligned() -> None:
    document = pymupdf.open(ASTERION_TESTER_PDF)
    try:
        assert len(document) == 4
        page_tables: list[dict[str, Any]] = []
        page_results: list[dict[str, Any]] = []
        for page_index, source_page in enumerate(document):
            has_native_text = bool(source_page.get_text("text").strip())
            page = source_page if has_native_text else run_ocr_for_page(source_page, dpi=150)
            result = extract_tables_from_page(page)
            page_results.append(result)
            table = next(
                item for item in result["tables"]
                if item.get("table_role") == "line_items" and item.get("column_count") == 6
            )
            page_tables.append(table)
            assert table["headers"] == ["S.NO", "DESCRIPTION", "UNIT", "QTY", "RATE", "AMT"]
            assert table["column_count"] == 6
            assert table["row_count"] == (16 if page_index in {0, 2} else 13)
            if page_index >= 2:
                assert len(table["cell_ocr_data"]) == table["row_count"]
                assert all(len(row) == 6 for row in table["cell_ocr_data"])
                assert all(
                    "raw_text" in cell and "confidence" in cell and "status" in cell
                    for row in table["cell_ocr_data"] for cell in row
                )
    finally:
        document.close()

    def keyed_rows(table: dict[str, Any]) -> dict[str, list[str]]:
        keyed: dict[str, list[str]] = {}
        for row in table["rows"]:
            match = re.match(r"\s*(\d+)", str(row[0]))
            assert match is not None
            item_number = match.group(1)
            assert item_number not in keyed
            assert str(row[0]).strip().rstrip(".") == item_number
            keyed[item_number] = row
        return keyed

    for digital_table, scanned_table in zip(page_tables[:2], page_tables[2:]):
        digital_rows = keyed_rows(digital_table)
        scanned_rows = keyed_rows(scanned_table)
        assert scanned_rows.keys() == digital_rows.keys()
        scan_row_indexes = {
            str(re.match(r"\s*(\d+)", str(row[0])).group(1)): row_index
            for row_index, row in enumerate(scanned_table["rows"])
        }
        for item_number, digital_row in digital_rows.items():
            scanned_row = scanned_rows[item_number]
            assert len(scanned_row) == len(digital_row) == 6
            # Description transcription can contain ordinary OCR substitutions;
            # structured values must stay aligned. A missed scan mark may remain
            # blank only when the exact cell is explicitly marked for review.
            row_index = scan_row_indexes[item_number]
            cell_flags = scanned_table.get("cell_review_flags", [])[row_index]
            for column_index in range(2, 6):
                if scanned_row[column_index] == digital_row[column_index]:
                    continue
                assert scanned_row[column_index] == "", item_number
                assert cell_flags[column_index], item_number

    digital_total = [
        (str(cell), str(row[index + 1]))
        for table in page_results[1]["tables"]
        for row in table.get("rows", [])
        for index, cell in enumerate(row[:-1])
        if "TOTAL AMOUNT USD" in str(cell).upper()
    ]
    scanned_total = [
        (item["label"], item["value"])
        for item in page_results[3]["summary_items"]
        if "TOTAL AMOUNT" in item.get("label", "").upper()
    ]
    assert any("TOTAL AMOUNT USD" in label.upper() and value == "1,582.66" for label, value in digital_total)
    assert ("TOTAL AMOUNT USD", "1,582.66") in scanned_total
    assert "TOTAL AMOUNT" not in str(page_tables[3]["rows"][-1][1]).upper()

    page_three_context = " ".join(
        str(row.get("text", "")) for row in page_tables[2].get("context_rows", [])
    )
    assert "CONTINUED ON NEXT PAGE" in page_three_context
