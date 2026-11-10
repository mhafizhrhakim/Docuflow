"""Regression tests for layout-driven, schema-independent table extraction."""
from __future__ import annotations

import pymupdf
from decimal import Decimal
from types import SimpleNamespace

from backend.services.table_extractor import (
    _annotate_colored_stamp_overlaps,
    _annotate_ocr_table_review,
    _attach_ocr_cell_evidence,
    _clean_cell,
    _clear_non_numeric_text_from_ocr_numeric_lanes,
    _detach_ocr_summary_candidates_from_item_rows,
    _extract_repeated_identifier_table,
    _merge_adjacent_header_fragments,
    _normalize_confusable_numeric_ocr_cells,
    _public_summary_items,
    _recover_missing_ocr_text_line_from_geometry,
    _recover_missing_numeric_cells_from_repeated_table_arithmetic,
    _split_header_runs_by_observed_lanes,
    _stamp_numeric_cell_crop_bbox,
    _without_browser_print_footer_lines,
    extract_tables_from_page,
)


def test_ocr_cell_evidence_preserves_description_punctuation_and_distinguishes_missing_text():
    page = SimpleNamespace(items=[
        {"text": "Valve", "x0": 12, "y0": 12, "x1": 28, "y1": 22, "confidence": 0.98},
        {"text": "O-ring", "x0": 30, "y0": 12, "x1": 52, "y1": 22, "confidence": 0.91},
        {"text": "/", "x0": 54, "y0": 12, "x1": 57, "y1": 22, "confidence": 0.88},
        {"text": "type-B", "x0": 59, "y0": 12, "x1": 81, "y1": 22, "confidence": 0.93},
        {"text": "0.75", "x0": 82, "y0": 12, "x1": 96, "y1": 22, "confidence": 0.95},
        {"text": "-", "x0": 102, "y0": 12, "x1": 105, "y1": 22, "confidence": 0.72},
    ])
    description = "Valve O-ring / type-B 0.75 mm"
    table = {
        "column_count": 4,
        "rows": [[description, "0.75", "", ""]],
        "cell_source_bboxes": [[
            [10, 10, 81, 24], [82, 10, 99, 24], [100, 10, 110, 24], [111, 10, 120, 24],
        ]],
    }

    _attach_ocr_cell_evidence(page, table)

    desc = table["cell_ocr_data"][0][0]
    assert desc["raw_text"] == "Valve O-ring / type-B"
    assert desc["final_value"] == description
    assert desc["status"] == "final_value_differs_from_raw_ocr"
    assert desc["confidence"] == 0.925
    assert desc["words"][2]["text"] == "/"
    assert table["cell_ocr_data"][0][1]["raw_text"] == "0.75"
    assert table["cell_ocr_data"][0][1]["confidence"] == 0.95
    assert table["cell_ocr_data"][0][2]["status"] == "ocr_text_not_assigned"
    assert table["cell_ocr_data"][0][2]["raw_text"] == "-"
    assert table["cell_ocr_data"][0][3]["status"] == "no_text_detected"
    assert table["cell_ocr_data"][0][3]["blankness"] == "unknown"
    assert _clean_cell("  O-ring / type-B: 0.75 mm.  ") == "O-ring / type-B: 0.75 mm."


def test_ocr_total_below_last_item_is_detached_from_description_and_kept_as_summary():
    page = SimpleNamespace(ocr_word_count=6, items=[
        {"text": "2", "x0": 10, "y0": 32, "x1": 14, "y1": 40, "confidence": 0.99},
        {"text": "Workwear", "x0": 21, "y0": 32, "x1": 46, "y1": 40, "confidence": 0.98},
        {"text": "specification", "x0": 48, "y0": 32, "x1": 76, "y1": 40, "confidence": 0.98},
        {"text": "TOTAL AMOUNT USD", "x0": 51, "y0": 62, "x1": 74, "y1": 70, "confidence": 0.95},
        {"text": "1,582.66", "x0": 80, "y0": 62, "x1": 96, "y1": 70, "confidence": 0.99},
    ])
    candidate = {
        "label": "TOTAL AMOUNT USD", "value": "1,582.66", "source_page": 1,
        "source_bbox": [50, 60, 98, 72], "_y": 66.0,
    }
    table = {
        "table_role": "line_items",
        "headers": ["No.", "Description", "Qty", "Unit Price", "Amount"],
        "rows": [
            ["1", "Safety gloves", "1", "7.20", "7.20"],
            ["2", "Workwear specification TOTAL AMOUNT USD : 1,582.66", "12", "7.20", "86.40"],
        ],
        "row_count": 2,
        "row_source_bboxes": [[5, 15, 130, 25], [5, 30, 130, 80]],
        "cell_source_bboxes": [
            [[5, 15, 18, 25], [18, 15, 75, 25], [75, 15, 90, 25], [90, 15, 110, 25], [110, 15, 130, 25]],
            [[5, 30, 18, 80], [18, 30, 100, 80], [100, 30, 110, 80], [110, 30, 120, 80], [120, 30, 130, 80]],
        ],
        "source_bbox": [5, 10, 130, 80],
    }

    data_bottom = _detach_ocr_summary_candidates_from_item_rows(
        page, [table], [candidate], 80.0,
    )

    assert table["row_count"] == 2  # Separation does not add or drop an item row.
    assert table["rows"][1] == ["2", "Workwear specification", "12", "7.20", "86.40"]
    assert table["row_source_bboxes"][1][3] == 59.0
    assert data_bottom == 59.0
    assert _public_summary_items([candidate], data_bottom) == [{
        key: value for key, value in candidate.items() if not key.startswith("_")
    }]


def _make_borderless_quote_page() -> tuple[pymupdf.Document, pymupdf.Page]:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    # Deliberately use unfamiliar column labels and no drawn table rules.
    lines = [
        (50, 110, "Item"),
        (120, 110, "Qty"),
        (190, 110, "Material Description"),
        (360, 110, "Unit Cost (JPY)"),
        (470, 110, "Quoted Amount (JPY)"),
        (50, 150, "1)"),
        (120, 150, "5 sets"),
        (190, 150, "Replacement valve assembly"),
        (500, 150, "2,400,000"),
        (50, 172, "2)"),
        (190, 172, "Freight"),
        (500, 172, "to be advised"),
        (360, 240, "Handling surcharge:"),
        (500, 240, "75,000"),
        (380, 260, "Quoted total:"),
        (500, 260, "2,475,000"),
    ]
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=10)
    return document, page


def test_borderless_table_discovers_dynamic_headers_and_integer_summaries() -> None:
    document, page = _make_borderless_quote_page()
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "table_found"
    assert len(result["tables"]) == 1
    table = result["tables"][0]
    assert table["headers"] == [
        "Item",
        "Qty",
        "Material Description",
        "Unit Cost (JPY)",
        "Quoted Amount (JPY)",
    ]
    assert table["row_count"] == 2
    assert table["rows"][0][0] == "1)"
    assert table["rows"][0][1] == "5 sets"
    assert table["rows"][0][2] == "Replacement valve assembly"
    assert table["rows"][0][4] == "2,400,000"
    assert table["rows"][1][2] == "Freight"
    assert table["rows"][1][4] == "to be advised"

    summaries = [(item["label"], item["value"]) for item in result["summary_items"]]
    assert ("Handling surcharge", "75,000") in summaries
    assert ("Quoted total", "2,475,000") in summaries
    # The amount on the item row must not be misclassified as a summary.
    assert ("Replacement valve assembly", "2,400,000") not in summaries


def test_borderless_item_table_is_not_hidden_by_small_metadata_grid() -> None:
    """A key/value grid must not suppress a larger borderless item table below it."""
    document = pymupdf.open()
    page = document.new_page(width=792, height=612)

    # Header metadata is deliberately ruled and table-shaped, while the item
    # rows below use only aligned text columns with no vertical grid lines.
    x_edges = [46, 168, 338, 434, 680, 746]
    y_edges = [86, 108, 130, 152]
    for y in y_edges:
        page.draw_line((x_edges[0], y), (x_edges[-1], y), width=0.5)
    for x in x_edges:
        page.draw_line((x, y_edges[0]), (x, y_edges[-1]), width=0.5)

    metadata = [
        (52, 101, "Order Number"), (174, 101, "ST-2026-002-QA"),
        (344, 101, "Vessel Name"), (440, 101, "MV TEST MERIDIAN"),
        (52, 123, "Contact Person"), (174, 123, "Nadia Pratama"),
        (344, 123, "Order Date"), (440, 123, "03-Nov-2026"),
        (52, 145, "Account"), (174, 145, "QA-5303002"),
        (344, 145, "Cost Code"), (440, 145, "ENG-01"),
    ]
    for x, y, text in metadata:
        page.insert_text((x, y), text, fontsize=9)
    page.insert_text(
        (52, 174),
        "Delivery Address: Test Vessel Store, Berth 04, Port of Example, Indonesia",
        fontsize=8,
    )
    page.insert_text(
        (52, 188),
        "Invoice Address: QA Marine Supplies Ltd., 88 Example Road, Jakarta 10110, Indonesia",
        fontsize=8,
    )

    headers = ["L.no", "Qty", "Unit", "Description", "IMPA Code", "Unit Price", "Total"]
    x_positions = [58, 105, 150, 207, 590, 660, 730]
    for x, text in zip(x_positions, headers):
        page.insert_text((x, 220), text, fontsize=8)
    rows = [
        ("10", "2", "PCS", "Air filter element, panel type, 400 x 200 mm", "813102", "0.00", "0.00"),
        ("11", "3", "PCS", "Elbow, butt-weld, 90 degree, long radius, DN50", "731126", "0.00", "0.00"),
        ("12", "4", "TIN", "Lubricant spray, multipurpose, 400 ml aerosol", "450702", "0.00", "0.00"),
        ("13", "6", "PCS", "Needle valve, stainless steel, BSPP, 1/2 inch", "756223", "0.00", "0.00"),
        ("14", "8", "PAIR", "Work gloves, cotton knitted, general purpose", "190104", "0.00", "0.00"),
        ("15", "10", "PCS", "Vernier caliper, stainless steel, 0-150 mm", "650502", "0.00", "0.00"),
        ("16", "12", "SET", "Diaphragm pump, air-operated, polypropylene body", "591767", "0.00", "0.00"),
    ]
    for row_index, row in enumerate(rows):
        y = 244 + row_index * 28
        for x, text in zip(x_positions, row):
            page.insert_text((x, y), text, fontsize=8)

    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "table_found"
    assert result["source_table_count"] == 1
    table = result["tables"][0]
    assert table["headers"] == headers
    assert table["row_count"] == len(rows)
    assert table["rows"][0] == list(rows[0])
    assert table["rows"][-1] == list(rows[-1])
    assert not any(
        item["label"] in headers or item["value"] in headers
        for item in result["metadata_items"]
    )
    metadata_pairs = {(item["label"], item["value"]) for item in result["metadata_items"]}
    assert (
        "Delivery Address",
        "Test Vessel Store, Berth 04, Port of Example, Indonesia",
    ) in metadata_pairs
    assert (
        "Invoice Address",
        "QA Marine Supplies Ltd., 88 Example Road, Jakarta 10110, Indonesia",
    ) in metadata_pairs


def test_unstructured_prose_is_not_returned_as_a_table() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 110), "We are pleased to offer the requested goods and services.", fontsize=10)
    page.insert_text((48, 140), "Delivery timing will be confirmed after the order is accepted.", fontsize=10)
    page.insert_text((48, 170), "Payment terms are thirty days from the invoice date.", fontsize=10)
    page.insert_text((48, 200), "Please contact our sales team if further information is required.", fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "no_table_detected"
    assert result["tables"] == []


def test_ruled_header_only_table_is_kept_without_inventing_item_rows() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.draw_line((42, 100), (590, 100), width=0.6)
    page.draw_line((42, 122), (590, 122), width=0.6)
    for x, label in [
        (48, "Record No."),
        (150, "Part Description"),
        (376, "Qty / Unit"),
        (452, "Unit Cost"),
        (532, "Line Value"),
    ]:
        page.insert_text((x, 116), label, fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "table_found"
    assert result["source_table_count"] == 1
    table = result["tables"][0]
    assert table["headers"] == [
        "Record No.", "Part Description", "Qty / Unit", "Unit Cost", "Line Value",
    ]
    assert table["header_detected"] is True
    assert table["row_count"] == 0
    assert table["rows"] == []
    assert table["column_count"] == 5
    assert table["source_bbox"] == [42.0, 100.0, 590.0, 122.0]


def test_column_positions_keep_adjacent_quantity_and_unit_in_separate_cells() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    # This reproduces a common borderless layout where adjacent cells have very
    # little whitespace and a simple word-gap split would merge Qty and Unit.
    lines = [
        (40, 110, "Line"),
        (100, 110, "Order Qty"),
        (180, 110, "Invoice Qty"),
        (222, 110, "Unit"),
        (255, 110, "Part Description"),
        (450, 110, "Unit Price"),
        (535, 110, "Line Amount"),
        (40, 150, "1"),
        (100, 150, "3.00"),
        (180, 150, "3.00"),
        (222, 150, "PCK"),
        (255, 150, "Detector tubes"),
        (450, 150, "206.70"),
        (535, 150, "620.10"),
        (40, 172, "2"),
        (100, 172, "2.00"),
        (180, 172, "2.00"),
        (222, 172, "SET"),
        (255, 172, "Safety valve"),
        (450, 172, "100.00"),
        (535, 172, "200.00"),
    ]
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "table_found"
    table = result["tables"][0]
    assert table["column_count"] == 7
    assert table["rows"][0][2] == "3.00"
    assert table["rows"][0][3] == "PCK"
    assert table["rows"][1][2] == "2.00"
    assert table["rows"][1][3] == "SET"


def test_compact_single_item_table_with_narrow_header_coverage_is_detected() -> None:
    """A short table can use only part of the page width and still be valid."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    lines = [
        (40, 418, "Item No"),
        (112, 418, "Qty"),
        (145, 418, "Unit"),
        (178, 418, "Description"),
        # The item number is indented inside its first cell. Qty and Unit are
        # tightly positioned and may be emitted by PDF text extraction as one run.
        (66, 434, "1"),
        (109, 434, "1.0000"),
        (145, 434, "SET"),
        (178, 434, "Safety valve"),
        # Detail text is wrapped/indented within the wide description cell.
        (172, 448, "Our Part No."),
        (245, 448, ": SGE-0044-010"),
        (40, 471, "TERMS & AGREEMENT"),
    ]
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "table_found"
    assert len(result["tables"]) == 1
    table = result["tables"][0]
    assert table["headers"] == ["Item No", "Qty", "Unit", "Description"]
    assert table["row_count"] == 1
    row = table["rows"][0]
    assert row[0] == "1"
    assert row[1] == "1.0000"
    assert row[2] == "SET"
    assert "Safety valve" in row[3]
    assert "Our Part No." in row[3]
    assert "SGE-0044-010" in row[3]
    assert "TERMS & AGREEMENT" not in row[3]


class _MergedFirstHeaderOCRPage:
    """Word-box fixture where identifier and description headers share a run."""

    rect = SimpleNamespace(width=1000.0, height=792.0, x0=0.0, y0=0.0, x1=1000.0, y1=792.0)
    number = 0
    ocr_word_count = 20
    ocr_elapsed_seconds = 0.01

    def __init__(self) -> None:
        self.words = []
        self._add("Record", 200, 180, 230, 200)
        self._add("Code", 232, 180, 252, 200)
        self._add("Supply", 260, 180, 300, 200)
        self._add("Cases", 400, 180, 440, 200)
        self._add("Measure", 520, 180, 570, 200)
        self._add("UnitCost", 650, 180, 710, 200)
        self._add("NetValue", 820, 180, 880, 200)
        for index, (description, quantity, measure, cost, amount) in enumerate([
            ("Pump assembly with seal", "3", "SET", "12.50", "37.50"),
            ("Replacement filter cartridge", "2", "BOX", "8.00", "16.00"),
        ], start=1):
            y0 = 220 + (index - 1) * 28
            self._add(str(index), 220, y0, 232, y0 + 18)
            self._add(description, 260, y0, 390, y0 + 18)
            self._add(quantity, 400, y0, 420, y0 + 18)
            self._add(measure, 520, y0, 550, y0 + 18)
            self._add(cost, 650, y0, 690, y0 + 18)
            self._add(amount, 820, y0, 860, y0 + 18)

    def _add(self, text, x0, y0, x1, y1):
        index = len(self.words)
        self.words.append((x0, y0, x1, y1, text, 0, index, index))

    def get_text(self, option="text", *args, **kwargs):
        if option == "words":
            return self.words
        if option == "dict":
            return {
                "width": self.rect.width,
                "height": self.rect.height,
                "blocks": [{
                    "type": 0,
                    "bbox": list(word[:4]),
                    "lines": [{"spans": [{"text": word[4], "bbox": list(word[:4])}]}],
                } for word in self.words],
            }
        if option == "text":
            return "\n".join(word[4] for word in self.words)
        return ""

    def find_tables(self, *args, **kwargs):
        return SimpleNamespace(tables=[])


def test_ocr_geometry_splits_first_header_using_identifier_and_description_lanes() -> None:
    result = extract_tables_from_page(_MergedFirstHeaderOCRPage())

    assert result["status"] == "table_found"
    table = result["tables"][0]
    assert table["headers"] == ["Record Code", "Supply", "Cases", "Measure", "UnitCost", "NetValue"]
    assert table["column_count"] == 6
    assert table["row_count"] == 2
    assert table["rows"][0] == ["1", "Pump assembly with seal", "3", "SET", "12.50", "37.50"]
    assert table["rows"][1] == ["2", "Replacement filter cartridge", "2", "BOX", "8.00", "16.00"]
    assert table["detection_strategy"] == "geometry_header_anchored"


def test_observed_lanes_ignore_a_one_off_text_start_inside_a_description_column() -> None:
    def word(text: str, x: float) -> dict[str, object]:
        return {"text": text, "x0": x, "x1": x + 8.0, "y0": 90.0, "y1": 100.0}

    def run(text: str, x: float, words: list[dict[str, object]] | None = None) -> dict[str, object]:
        return {"text": text, "x0": x, "x1": x + 30.0, "y0": 90.0, "y1": 100.0, "words": words or [word(text, x)]}

    header = {
        "y": 100.0,
        "x0": 50.0,
        "x1": 562.0,
        "runs": [
            run("Record Product", 50.0, [word("Record", 50.0), word("Product", 73.0)]),
            run("Weight", 375.0),
            run("Unit", 408.0),
            run("Pack", 475.0),
            run("Count", 532.0),
        ],
    }
    lines = []
    for row_index in range(8):
        y = 125.0 + row_index * 18.0
        runs = [
            run(str(row_index + 1), 53.0),
            run("Product description", 73.0),
            run(str(row_index + 2), 375.0),
            run("SET", 408.0),
            run("BOX", 475.0),
            run("12", 532.0),
        ]
        if row_index == 0:
            # A wrapped description line starts farther to the right once; it
            # must not become a new document-wide column lane.
            runs.insert(2, run("continued", 120.0))
        lines.append({"y": y, "runs": runs})

    rebuilt = _split_header_runs_by_observed_lanes(header, lines)

    assert rebuilt["observed_data_lanes"] == [53.0, 73.0, 375.0, 408.0, 475.0, 532.0]
    assert [entry["text"] for entry in rebuilt["runs"][:2]] == ["Record", "Product"]


def test_ocr_observed_lanes_ignore_repeated_numeric_reference_inside_description() -> None:
    def word(text: str, x: float, y: float) -> dict[str, object]:
        return {"text": text, "x0": x, "x1": x + max(8.0, len(text) * 4.0), "y0": y, "y1": y + 8.0}

    def run(text: str, x: float, y: float) -> dict[str, object]:
        item = word(text, x, y)
        return {**item, "words": [item]}

    header_y = 100.0
    header = {
        "y": header_y, "x0": 50.0, "x1": 535.0,
        "runs": [
            run("S.NO", 52.0, header_y), run("DESCRIPTION", 198.0, header_y),
            run("UNIT", 375.0, header_y), run("QTY", 418.0, header_y),
            run("RATE", 463.0, header_y), run("AMT", 517.0, header_y),
        ],
    }
    lines = []
    for index in range(10):
        y = 125.0 + index * 22.0
        lines.append({
            "y": y, "x0": 47.0, "x1": 530.0,
            "runs": [
                run(str(index + 1), 47.0, y),
                run(f"REPLACEMENT ITEM {index + 1}", 81.0, y),
                # Repeated catalog-like numbers start inside the wide prose
                # lane and must not create a phantom table column.
                run(str(7500000 + index), 226.0, y),
                run("PCS", 364.0, y), run("2", 406.0, y),
                run("1.25", 450.0, y), run("2.50", 501.0, y),
            ],
        })

    rebuilt = _split_header_runs_by_observed_lanes(header, lines, ocr_page=True)

    assert len(rebuilt["observed_data_lanes"]) == 6
    assert rebuilt["observed_data_lanes"] == [47.0, 81.0, 364.0, 406.0, 450.0, 501.0]


def test_repeated_ocr_header_assigns_boundary_straddling_label_by_word_center() -> None:
    page = SimpleNamespace(rect=SimpleNamespace(width=595.0, height=842.0))

    def run(text: str, x0: float, x1: float, y: float) -> dict[str, object]:
        word = {"text": text, "x0": x0, "x1": x1, "y0": y - 4, "y1": y + 4}
        return {
            "text": text, "x0": x0, "x1": x1, "y0": y - 4, "y1": y + 4,
            "words": [word],
        }

    header_y = 100.0
    header_runs = [
        run("No", 50, 64, header_y),
        run("Description", 70, 130, header_y),
        run("Type", 370, 394, header_y),
        run("Requested", 398, 453, header_y),
        run("Qty", 410, 432, header_y),
        run("Price", 455, 480, header_y),
        run("USD", 480, 494, header_y),
        # Its left edge falls left of the Price/Amount midpoint, while its
        # center lies correctly within the Amount lane.
        run("Amount", 488, 518, header_y),
        run("USD", 530, 550, header_y),
    ]
    lines = [{"y": header_y, "x0": 50.0, "x1": 550.0, "runs": header_runs}]
    for index in range(1, 5):
        y = 125.0 + (index - 1) * 24.0
        cells = [
            (str(index), 50, 62),
            (f"ITEM DESCRIPTION {index}", 70, 190),
            ("BOX", 370, 392),
            ("2", 405, 416),
            ("1.25", 460, 482),
            ("2.50", 530, 554),
        ]
        row_runs = [run(text, x0, x1, y) for text, x0, x1 in cells]
        lines.append({"y": y, "x0": 50.0, "x1": 554.0, "runs": row_runs})

    table, _ = _extract_repeated_identifier_table(page, lines)

    assert table is not None
    assert table["headers"] == [
        "No", "Description", "Type", "Requested Qty", "Price USD", "Amount USD",
    ]


def test_repeated_ocr_header_merges_x_aligned_fragment_from_neighboring_baseline() -> None:
    headers = ["No", "Description", "Type", "Requested", "Price USD", "Amount USD"]
    primary_words = [
        [{"text": "No", "x0": 52.3, "y0": 233.2, "x1": 64.3, "y1": 245.2}],
        [{"text": "Description", "x0": 204.4, "y0": 231.7, "x1": 236.5, "y1": 245.2}],
        [{"text": "Type", "x0": 375.2, "y0": 230.8, "x1": 387.7, "y1": 245.2}],
        [{"text": "Requested", "x0": 399.7, "y0": 227.9, "x1": 429.9, "y1": 239.4}],
        [{"text": "Price", "x0": 449.1, "y0": 231.3, "x1": 462.1, "y1": 242.8},
         {"text": "USD", "x0": 464.5, "y0": 231.3, "x1": 475.5, "y1": 242.8}],
        [{"text": "Amount", "x0": 502.4, "y0": 230.3, "x1": 525.9, "y1": 242.3},
         {"text": "USD", "x0": 527.3, "y0": 229.8, "x1": 539.3, "y1": 241.8}],
    ]
    primary_line = {"y": 237.02}
    supplemental_line = {
        "y": 242.3,
        "runs": [{
            "text": "Qty", "x0": 409.3, "y0": 236.5, "x1": 420.8, "y1": 248.1,
            "words": [{"text": "Qty", "x0": 409.3, "y0": 236.5, "x1": 420.8, "y1": 248.1}],
        }],
    }
    boundaries = [128.0, 289.8, 387.45, 424.4, 475.75]

    merged, fragment_boxes = _merge_adjacent_header_fragments(
        headers, primary_words, primary_line, [primary_line, supplemental_line], boundaries, 595.0,
    )

    assert merged[3] == "Requested (Qty)"
    assert merged[:3] == headers[:3]
    assert merged[4:] == headers[4:]
    assert fragment_boxes[3] == [[409.3, 236.5, 420.8, 248.1]]


def test_footer_marker_with_adjacent_ocr_noise_is_removed_from_table_geometry() -> None:
    lines = [
        {"y": 760.0, "runs": [{"text": "172"}, {"text": "TUNA FLAKES"}]},
        {"y": 771.0, "runs": [{"text": "about:blank"}, {"text": "TA*"}, {"text": "4/5"}]},
        {"y": 600.0, "runs": [{"text": "about:blank"}]},
    ]

    filtered = _without_browser_print_footer_lines(lines, page_height=842.0)

    assert filtered == [lines[0], lines[2]]


class _HeaderlessObservedLaneOCRPage(_MergedFirstHeaderOCRPage):
    """Headerless continuation-page geometry modeled on the failing scan."""

    rect = SimpleNamespace(width=595.0, height=842.0, x0=0.0, y0=0.0, x1=595.0, y1=842.0)
    ocr_word_count = 360

    def __init__(self) -> None:
        self.words = []
        rows = [
            (48, "MOZZARELLA CHEESE SHREDDED", "1", "KG", "PACK", "10"),
            (49, "CHEDDAR CHEESE SLICES", "250", "GM", "PACK", "20"),
            (50, "WHIPPING CREAM", "1", "LTR", "PIECE", "10"),
            (51, "LONG LIFE FULL CREAM MILK", "1", "LTR", "PIECE", "60"),
            (52, "LONG LIFE MILK LOW FAT", "1", "LTR", "PIECE", "50"),
            (53, "ICE CREAM BAR ASSORTED FLAVOR", "1", "NO", "PIECE", "5"),
        ]
        rows.extend(
            (identifier, f"PRODUCT ITEM {identifier}", "1", "KG", "PACK", str(identifier % 23 + 1))
            for identifier in range(54, 108)
        )
        for index, (identifier, description, measure, unit, package, quantity) in enumerate(rows):
            y = 141.5 + index * 9.7
            drift = 0.041 * (y - 141.5)
            id_x = 104.1 if identifier == 52 else 104.6 + drift
            self._add(str(identifier), id_x, y, id_x + 7.4, y + 10.0)
            x = 126.1 + drift
            for word in description.split():
                width = max(13.0, len(word) * 5.2)
                self._add(word, x, y, x + width, y + 10.0)
                x += width + 3.0
            for value, x, width in [
                (measure, 360.2 + drift, 11.0),
                (unit, 398.6 + drift, 14.0),
                (package, 436.0 + drift, 18.0),
                (quantity, 484.0 + drift, 12.0),
            ]:
                self._add(value, x, y, x + width, y + 10.0)


class _OCRPageWithNumericConfusions(_MergedFirstHeaderOCRPage):
    """Headerless scan geometry with the two OCR artifacts seen in a real page."""

    rect = SimpleNamespace(width=595.0, height=842.0, x0=0.0, y0=0.0, x1=595.0, y1=842.0)

    def __init__(self) -> None:
        self.words = []
        identifiers = [*range(171, 209), *range(210, 232)]
        for index, identifier in enumerate(identifiers):
            y = 126.5 + index * 9.8
            drift = 0.034 * (y - 126.5)
            self._add(str(identifier), 105.4 + drift, y, 116.0 + drift, y + 10.0)
            description = f"GROCERY PRODUCT {identifier}"
            x = 126.0 + drift
            for word in description.split():
                width = max(13.0, len(word) * 5.2)
                self._add(word, x, y, x + width, y + 10.0)
                x += width + 3.0
            weight = "S0O" if identifier == 183 else "500"
            quantity = "‘2" if identifier == 201 else "10"
            for value, x, width in [
                (weight, 360.2 + drift, 20.0),
                ("GM", 398.6 + drift, 14.0),
                ("PACK", 436.0 + drift, 22.0),
                (quantity, 484.0 + drift, 14.0),
            ]:
                self._add(value, x, y, x + width, y + 10.0)
        self.ocr_word_count = len(self.words)


def test_headerless_ocr_uses_repeated_body_lanes_instead_of_merging_columns() -> None:
    result = extract_tables_from_page(_HeaderlessObservedLaneOCRPage())

    assert result["status"] == "table_found"
    table = result["tables"][0]
    assert table["header_detected"] is False
    assert table["column_count"] == 6
    assert table["row_count"] == 60
    assert table["rows"][0] == ["48", "MOZZARELLA CHEESE SHREDDED", "1", "KG", "PACK", "10"]
    assert table["rows"][1] == ["49", "CHEDDAR CHEESE SLICES", "250", "GM", "PACK", "20"]
    assert table["rows"][4] == ["52", "LONG LIFE MILK LOW FAT", "1", "LTR", "PIECE", "50"]
    assert table["rows"][-1] == ["107", "PRODUCT ITEM 107", "1", "KG", "PACK", "16"]
    assert all(len(row) == 6 for row in table["rows"])
    assert table["detection_strategy"] == "ocr_repeated_row_geometry"
    assert table["review_required"] is True
    assert all(table["rows_need_review"])
    header_issue = next(issue for issue in table["review_issues"] if issue["code"] == "header_not_detected")
    assert "dibentuk dari posisi isi OCR" in header_issue["message"]
    assert "semua baris ditandai untuk diperiksa" in header_issue["message"]


def test_ocr_numeric_quote_noise_does_not_split_long_table_and_confusable_digits_are_reviewed() -> None:
    result = extract_tables_from_page(_OCRPageWithNumericConfusions())

    assert result["status"] == "table_found"
    assert len(result["metadata_items"]) == 0
    table = result["tables"][0]
    assert table["column_count"] == 6
    assert table["row_count"] == 60
    assert table["rows"][0][0] == "171"
    assert table["rows"][-1][0] == "231"
    assert all(len(row) == 6 for row in table["rows"])
    rows_by_id = {row[0]: index for index, row in enumerate(table["rows"])}
    weight_row = rows_by_id["183"]
    quantity_row = rows_by_id["201"]
    assert table["rows"][weight_row][2] == "500"
    assert table["rows"][quantity_row][5] == "2"
    normalized = [issue for issue in table["review_issues"] if issue["code"] == "ocr_numeric_confusable_normalized"]
    assert {(issue["raw_text"], issue["normalized_text"]) for issue in normalized} == {
        ("S0O", "500"), ("‘2", "2"),
    }
    assert all(issue["source_bbox"] for issue in normalized)
    assert table["cell_review_flags"][weight_row][2] is True
    assert table["rows_need_review"][weight_row] is True
    assert "209" not in rows_by_id  # Do not fabricate an item absent from OCR.


def test_standalone_s_is_normalized_to_five_only_in_observed_numeric_lane():
    quantities = ["2", "5", "5", "s", "5", "10"]
    rows = [[str(index), f"Item {index}", value] for index, value in enumerate(quantities, start=1)]
    cell_boxes = [
        [[10, 10 + index * 20, 20, 20 + index * 20],
         [25, 10 + index * 20, 80, 20 + index * 20],
         [90, 10 + index * 20, 110, 20 + index * 20]]
        for index in range(len(rows))
    ]
    table = {
        "column_count": 3,
        "headers": ["No.", "Item", "Requested"],
        "header_detected": True,
        "rows": rows,
        "cell_source_bboxes": cell_boxes,
    }
    page = SimpleNamespace(ocr_word_count=18, items=[])

    _annotate_ocr_table_review(page, table)
    _normalize_confusable_numeric_ocr_cells(page, table)

    assert table["rows"][3][2] == "5"
    assert table["rows"][2][2] == "5"
    issue = next(
        issue for issue in table["review_issues"]
        if issue["code"] == "ocr_numeric_confusable_normalized"
    )
    assert (issue["raw_text"], issue["normalized_text"]) == ("s", "5")
    assert issue["row_index"] == 3
    assert issue["column_index"] == 2
    assert table["cell_review_flags"][3][2] is True
    assert table["rows_need_review"][3] is True
    assert not any(
        issue.get("code") == "unexpected_text_in_numeric_lane"
        and issue.get("row_index") == 3
        and issue.get("column_index") == 2
        for issue in table["review_issues"]
    )


def test_ocr_review_reports_sequence_gap_without_inventing_a_row() -> None:
    rows = [[str(value), f"Item {value}"] for value in (10, 11, 12, 14, 15, 16)]
    table = {
        "column_count": 2,
        "headers": ["No", "Item"],
        "header_detected": True,
        "rows": rows,
        "row_source_bboxes": [[100, 100 + index * 20, 300, 110 + index * 20] for index in range(len(rows))],
    }
    page = SimpleNamespace(items=[])

    _annotate_ocr_table_review(page, table)

    assert len(table["rows"]) == 6
    assert all("13" not in row for row in table["rows"])
    gap = next(issue for issue in table["review_issues"] if issue["code"] == "possible_sequence_gap")
    assert gap["identifiers"] == ["13"]
    assert "bukan bukti" in gap["message"]
    assert table["rows_need_review"][2] is True
    assert table["rows_need_review"][3] is True


def test_ocr_review_does_not_flag_intentionally_blank_cells_without_source_text() -> None:
    table = {
        "column_count": 2,
        "headers": ["No.", "Remarks"],
        "header_detected": True,
        "rows": [["1", ""]],
        "row_source_bboxes": [[0, 0, 100, 20]],
        "cell_source_bboxes": [[[0, 0, 20, 20], [21, 0, 100, 20]]],
    }

    _annotate_ocr_table_review(SimpleNamespace(items=[]), table)

    assert table["cell_review_flags"] == [[False, False]]
    assert table["rows_need_review"] == [False]
    assert table["review_issues"] == []


def test_ocr_review_flags_empty_cell_only_when_ocr_text_is_in_its_source_area() -> None:
    table = {
        "column_count": 2,
        "headers": ["No.", "Remarks"],
        "header_detected": True,
        "rows": [["1", ""]],
        "row_source_bboxes": [[0, 0, 100, 20]],
        "cell_source_bboxes": [[[0, 0, 20, 20], [21, 0, 100, 20]]],
    }
    page = SimpleNamespace(items=[{
        "text": "some note", "x0": 40, "y0": 5, "x1": 75, "y1": 15,
        "confidence": 0.93,
    }])

    _annotate_ocr_table_review(page, table)

    assert table["cell_review_flags"] == [[False, True]]
    assert table["rows_need_review"] == [True]
    issue = next(issue for issue in table["review_issues"] if issue["code"] == "ocr_text_not_assigned_to_cell")
    assert issue["column_index"] == 1
    assert "nilai hasil ekstraksi kosong" in issue["message"]


def test_ocr_geometry_keeps_close_header_and_tracks_slanted_column_lanes() -> None:
    page = _SlantedOCRTablePage()
    result = extract_tables_from_page(page)

    assert result["status"] == "table_found"
    table = result["tables"][0]
    assert table["headers"] == ["Record No.", "Product", "Weight", "Unit", "Pack", "Count"]
    assert table["column_count"] == 6
    assert table["row_count"] == 18
    assert all(len(row) == 6 for row in table["rows"])
    assert table["rows"][0] == ["1", "Valve assembly 1", "3", "SET", "BOX", "12"]
    assert table["rows"][-1] == ["18", "Valve assembly 18", "20", "SET", "BOX", "29"]
    # Either geometry path may win when it reconstructs the same complete,
    # source-aligned rows; the selected internal strategy is not API behavior.
    assert table["detection_strategy"] in {
        "geometry_header_anchored", "ocr_repeated_row_geometry",
    }


def test_colored_stamp_overlap_clears_numeric_lane_noise_and_preserves_raw_ocr():
    rows = [[str(index), value] for index, value in enumerate(("1", "2", "3", "4", "s"), start=1)]
    table = {
        "column_count": 2,
        "headers": ["Item", "Qty"],
        "rows": rows,
        "rows_need_review": [False, False, False, False, True],
        "row_review_reasons": [[], [], [], [], ["unexpected_text_in_numeric_lane"]],
        "cell_source_bboxes": [
            [[10, 10 + index * 20, 90, 30 + index * 20], [90, 10 + index * 20, 120, 30 + index * 20]]
            for index in range(5)
        ],
    }
    page = SimpleNamespace(get_stamp_regions=lambda: [[96, 92, 115, 108]])

    _annotate_colored_stamp_overlaps(page, table)

    assert table["rows"][4][1] == ""
    assert table["cell_review_flags"][4] == [False, True]
    assert "possible_colored_stamp_overlap" in table["row_review_reasons"][4]
    assert table["review_required"] is True
    issue = next(
        issue for issue in table["review_issues"]
        if issue["code"] == "possible_colored_stamp_overlap"
    )
    assert issue["row_index"] == 4
    assert issue["column_index"] == 1
    assert issue["raw_ocr_value"] == "s"
    assert issue["value_cleared"] is True


def test_colored_stamp_crop_recovers_numeric_value_and_replaces_stale_lane_issue(monkeypatch):
    rows = [
        [str(index + 1), value]
        for index, value in enumerate(["2", "5", "MESOTK", "5", "5", "10"])
    ]
    cell_boxes = [
        [[10, 10 + index * 20, 30, 28 + index * 20],
         [90, 10 + index * 20, 120, 28 + index * 20]]
        for index in range(len(rows))
    ]
    table = {
        "column_count": 2,
        "headers": ["No.", "Requested Qty"],
        "rows": rows,
        "cell_source_bboxes": cell_boxes,
        "rows_need_review": [False, False, True, False, False, False],
        "row_review_reasons": [[], [], ["unexpected_text_in_numeric_lane"], [], [], []],
        "cell_review_flags": [[False, False] for _ in rows],
        "review_issues": [{
            "code": "unexpected_text_in_numeric_lane",
            "message": "OCR menghasilkan teks pada lane numerik.",
            "row_index": 2,
            "column_index": 1,
            "value": "MESOTK",
        }],
    }
    page = SimpleNamespace(get_stamp_regions=lambda: [[95, 52, 115, 65]])
    monkeypatch.setattr(
        "backend.services.ocr_adapter.recognize_stamp_occluded_numeric_crop",
        lambda _page, _bbox: {"text": "5", "confidence": 0.94},
    )

    _annotate_colored_stamp_overlaps(page, table)

    assert table["rows"][2][1] == "5"
    assert table["rows_need_review"][2] is True
    assert table["cell_review_flags"][2][1] is True
    assert "possible_colored_stamp_overlap" in table["row_review_reasons"][2]
    assert "unexpected_text_in_numeric_lane" not in table["row_review_reasons"][2]
    assert len(table["review_issues"]) == 1
    issue = table["review_issues"][0]
    assert issue["code"] == "possible_colored_stamp_overlap"
    assert issue["raw_ocr_value"] == "MESOTK"
    assert issue["recovered_value"] == "5"
    assert issue["recovery_confidence"] == 0.94
    assert issue["value_recovered"] is True
    assert issue["value_cleared"] is False


def test_stamp_crop_bbox_uses_repeated_ocr_word_lanes_when_cell_boxes_are_missing():
    rows = [
        [str(index), f"Item {index}", unit, quantity, price, amount]
        for index, (unit, quantity, price, amount) in enumerate([
            ("KG", "2", "1.25", "2.50"),
            ("KG", "5", "8.14", "40.70"),
            ("KG", "10", "7.32", "73.20"),
            ("KG", "3", "4.50", "13.50"),
        ], start=1)
    ]
    row_boxes = [[50, 100 + index * 18, 550, 116 + index * 18] for index in range(len(rows))]
    words = []
    for index, row in enumerate(rows):
        y = 108 + index * 18
        for text, x0, x1 in [
            (row[0], 52, 62), (row[1], 75, 180), (row[2], 375, 390),
            (row[3], 410, 422), (row[4], 477, 491), (row[5], 531, 548),
        ]:
            words.append({"text": text, "x0": x0, "x1": x1, "y0": y - 5, "y1": y + 5, "confidence": 0.98})
    table = {
        "rows": rows,
        "row_source_bboxes": row_boxes,
        "cell_source_bboxes": [],
    }
    page = SimpleNamespace(items=words, rect=SimpleNamespace(x0=0, y0=0, x1=595, y1=842))

    bbox = _stamp_numeric_cell_crop_bbox(table, 2, 3, page)

    assert bbox is not None
    assert 390 <= bbox[0] < 410
    assert 422 < bbox[2] <= 450
    assert bbox[1] < 144 < bbox[3]


def test_stamp_crop_uses_header_lane_and_first_row_baseline_for_multi_line_rows(monkeypatch):
    codes = ["734725", "734721", "734719", "X11014", "711013", "711008", "711006"]
    rows = [
        [str(index + 9) + ".", f"Pipe catalog {code}, IMPA NO: {code}", code, "2", "PCS"]
        for index, code in enumerate(codes)
    ]
    row_boxes = [
        [18, 180 + index * 28, 562, 206 + index * 28]
        for index in range(len(rows))
    ]
    words = [
        {"text": "NO.", "x0": 19, "y0": 80, "x1": 32, "y1": 92, "confidence": 0.99},
        {"text": "DESCRIPTION", "x0": 54, "y0": 80, "x1": 104, "y1": 92, "confidence": 0.99},
        {"text": "CODE", "x0": 438, "y0": 80, "x1": 455, "y1": 92, "confidence": 0.99},
        {"text": "Q'TY", "x0": 525, "y0": 80, "x1": 543, "y1": 92, "confidence": 0.99},
        {"text": "UNIT", "x0": 548, "y0": 80, "x1": 565, "y1": 92, "confidence": 0.99},
    ]
    for index, row in enumerate(rows):
        y = 187 + index * 28
        words.extend([
            {"text": row[0], "x0": 19, "y0": y, "x1": 34, "y1": y + 11, "confidence": 0.99},
            {"text": "Pipe", "x0": 55, "y0": y, "x1": 78, "y1": y + 11, "confidence": 0.99},
            {"text": row[2], "x0": 95, "y0": y, "x1": 122, "y1": y + 11, "confidence": 0.99},
            {"text": row[2], "x0": 175, "y0": y, "x1": 202, "y1": y + 11, "confidence": 0.99},
            {"text": row[2], "x0": 437, "y0": y, "x1": 464, "y1": y + 11, "confidence": 0.99},
            {"text": row[3], "x0": 534, "y0": y, "x1": 544, "y1": y + 11, "confidence": 0.99},
            {"text": row[4], "x0": 548, "y0": y, "x1": 563, "y1": y + 11, "confidence": 0.99},
        ])
    captured = {}
    def read_crop(_page, bbox):
        captured["bbox"] = bbox
        return {"text": "711014", "confidence": 0.96}

    monkeypatch.setattr(
        "backend.services.ocr_adapter.recognize_stamp_occluded_numeric_crop",
        read_crop,
    )
    table = {
        "headers": ["NO.", "DESCRIPTION", "CODE", "Q'TY", "UNIT"],
        "column_count": 5,
        "rows": rows,
        "header_source_bboxes": [[18, 78, 565, 95]],
        "row_source_bboxes": row_boxes,
        "cell_source_bboxes": [],
        "rows_need_review": [False, False, False, True, False, False, False],
        "row_review_reasons": [[], [], [], ["unexpected_text_in_numeric_lane"], [], [], []],
        "review_issues": [{
            "code": "unexpected_text_in_numeric_lane",
            "row_index": 3,
            "column_index": 2,
            "value": "X11014",
        }],
    }
    page = SimpleNamespace(
        rect=SimpleNamespace(x0=0, y0=0, x1=595, y1=842),
        items=words,
        get_stamp_regions=lambda: [[424, 260, 480, 290]],
    )

    _annotate_colored_stamp_overlaps(page, table)

    assert table["rows"][3][2] == "711014"
    assert 420 <= captured["bbox"][0] < 430
    assert 465 <= captured["bbox"][2] <= 475
    assert captured["bbox"][1] < 271 < captured["bbox"][3]
    issue = next(issue for issue in table["review_issues"] if issue.get("row_index") == 3)
    assert issue["raw_ocr_value"] == "X11014"
    assert issue["recovered_value"] == "711014"
    assert issue["value_recovered"] is True


def test_missing_numeric_cell_recovers_only_when_repeated_table_arithmetic_supports_it():
    rows = []
    for index in range(12):
        quantity = index + 2
        price = Decimal("1.25") + Decimal(index) / Decimal("2")
        amount = (Decimal(quantity) * price).quantize(Decimal("0.01"))
        rows.append([
            str(index + 1), f"Supply item {index + 1}", "BOX",
            str(quantity), format(price, "f"), format(amount, ".2f"),
        ])
    missing_row = 6
    expected = rows[missing_row][3]
    rows[missing_row][3] = ""
    table = {
        "column_count": 6,
        "rows": rows,
        "rows_need_review": [index == missing_row for index in range(len(rows))],
        "row_review_reasons": [
            ["possible_colored_stamp_overlap"] if index == missing_row else []
            for index in range(len(rows))
        ],
        "cell_review_flags": [[False] * 6 for _ in rows],
        "review_issues": [{
            "code": "possible_colored_stamp_overlap",
            "message": "Nilai OCR sel di lane angka dihapus sementara.",
            "row_index": missing_row,
            "column_index": 3,
            "raw_ocr_value": "ELGO#",
            "value_cleared": True,
        }],
    }

    _recover_missing_numeric_cells_from_repeated_table_arithmetic(table)

    assert table["rows"][missing_row][3] == expected
    assert table["rows_need_review"][missing_row] is True
    assert table["cell_review_flags"][missing_row][3] is True
    issue = next(issue for issue in table["review_issues"] if issue["row_index"] == missing_row)
    assert issue["raw_ocr_value"] == "ELGO#"
    assert issue["recovered_value"] == expected
    assert issue["recovery_method"] == "repeated_table_arithmetic"
    assert issue["support_ratio"] == 1.0
    assert table["review_required"] is True


def test_missing_numeric_cell_stays_blank_without_repeated_arithmetic_evidence():
    rows = [[str(index + 1), f"Item {index}", str(index + 2), str(3 + index * 1.37), str(99 + index)] for index in range(10)]
    rows[4][2] = ""
    table = {
        "column_count": 5,
        "rows": rows,
        "review_issues": [{
            "code": "possible_colored_stamp_overlap",
            "row_index": 4,
            "column_index": 2,
            "raw_ocr_value": "MASTER",
            "value_cleared": True,
        }],
    }

    _recover_missing_numeric_cells_from_repeated_table_arithmetic(table)

    assert table["rows"][4][2] == ""
    assert not table.get("review_required", False)


def test_colored_stamp_overlap_falls_back_to_row_and_word_geometry_without_cell_boxes():
    values = ["730001", "730002", "730003", "730004", "730005", "730006", "730007", "730008", "734719 GAS", "X11014"]
    table = {
        "column_count": 2,
        "headers": ["No.", "Code"],
        "rows": [[str(index + 1), value] for index, value in enumerate(values)],
        "row_source_bboxes": [
            [10, 10 + index * 20, 300, 28 + index * 20]
            for index in range(len(values))
        ],
        "cell_source_bboxes": [],
        "rows_need_review": [False] * len(values),
        "row_review_reasons": [[] for _ in values],
    }
    page = SimpleNamespace(
        get_stamp_regions=lambda: [[80, 170, 100, 210]],
        items=[
            {"text": "GAS", "x0": 84, "y0": 174, "x1": 96, "y1": 184},
            {"text": "X11014", "x0": 82, "y0": 194, "x1": 98, "y1": 204},
        ],
    )

    _annotate_colored_stamp_overlaps(page, table)

    assert table["rows"][8][1] == ""
    assert table["rows"][9][1] == ""
    assert table["rows"][7][1] == "730008"
    issues = [
        issue for issue in table["review_issues"]
        if issue["code"] == "possible_colored_stamp_overlap"
    ]
    assert {(issue["row_index"], issue["column_index"]) for issue in issues} == {(8, 1), (9, 1)}
    assert {issue["raw_ocr_value"] for issue in issues} == {"734719 GAS", "X11014"}
    assert all(issue["geometry_fallback"] == "row_and_overlapping_words" for issue in issues)


def _make_missing_first_description_line_case():
    headers = ["No.", "Material", "Reference", "Amount", "Measure"]
    rows = []
    row_boxes = []
    items = [
        {"text": "No.", "x0": 20, "y0": 80, "x1": 34, "y1": 92},
        {"text": "Material", "x0": 55, "y0": 80, "x1": 105, "y1": 92},
        {"text": "Reference", "x0": 438, "y0": 80, "x1": 472, "y1": 92},
        {"text": "Amount", "x0": 520, "y0": 80, "x1": 540, "y1": 92},
        {"text": "Measure", "x0": 548, "y0": 80, "x1": 565, "y1": 92},
    ]
    missing_row = 3
    for index in range(7):
        top = 100 + index * 25
        row_boxes.append([18, top, 570, top + 23])
        reference = str(800 + index)
        description = (
            f"Catalog reference {reference}"
            if index == missing_row
            else f"Replacement wrench assembly {index + 1} size 17mm"
        )
        rows.append([f"{index + 1}.", description, reference, str(index + 2), "PCS"])
        baseline = top + 2
        items.extend([
            {"text": f"{index + 1}.", "x0": 22, "y0": baseline, "x1": 35, "y1": baseline + 13},
            {"text": reference, "x0": 438, "y0": baseline, "x1": 464, "y1": baseline + 13},
            {"text": str(index + 2), "x0": 530, "y0": baseline, "x1": 540, "y1": baseline + 13},
            {"text": "PCS", "x0": 550, "y0": baseline, "x1": 565, "y1": baseline + 13},
        ])
        if index == missing_row:
            # Full-page OCR missed the first description baseline but kept the
            # lower continuation in the correct text lane.
            for token_index, token in enumerate(description.split()):
                left = 56 + token_index * 31
                items.append({
                    "text": token,
                    "x0": left,
                    "y0": baseline + 10,
                    "x1": left + 27,
                    "y1": baseline + 22,
                })
        else:
            text = description.split()
            for token_index, token in enumerate(text):
                left = 56 + token_index * 42
                items.append({
                    "text": token,
                    "x0": left,
                    "y0": baseline,
                    "x1": left + 37,
                    "y1": baseline + 13,
                })
    return rows, row_boxes, items, missing_row


def test_missing_first_text_line_is_recovered_from_locally_confirmed_geometry(monkeypatch):
    rows, row_boxes, items, missing_row = _make_missing_first_description_line_case()
    rows[missing_row][1] = "Catalog reference 803"
    rows[missing_row][2] = ""
    items = [
        item for item in items
        if not (item.get("text") == "803" and item.get("x0") == 438)
    ]
    table = {
        "column_count": 5,
        "headers": ["No.", "Material", "Reference", "Amount", "Measure"],
        "header_source_bboxes": [[18, 78, 570, 95]],
        "rows": rows,
        "row_source_bboxes": row_boxes,
    }
    page = SimpleNamespace(
        rect=SimpleNamespace(x0=0, y0=0, x1=595, y1=842),
        items=items,
        ocr_word_count=len(items),
    )
    captured = []

    def read_crop(_page, bbox):
        captured.append(bbox)
        if bbox[2] - bbox[0] < 100:
            return {"text": "803", "confidence": 0.99}
        return {"text": "Replacement wrench assembly 4 size 17mm", "confidence": 0.98}

    monkeypatch.setattr(
        "backend.services.ocr_adapter.recognize_ocr_text_line_crop", read_crop
    )

    _recover_missing_ocr_text_line_from_geometry(page, table)

    assert rows[missing_row][1] == (
        "Replacement wrench assembly 4 size 17mm Catalog reference 803"
    )
    assert rows[missing_row][2] == "803"
    assert table["rows_need_review"][missing_row] is True
    assert table["cell_review_flags"][missing_row][1] is True
    assert table["cell_review_flags"][missing_row][2] is True
    assert table["validation_status"] == "needs_review"
    issue = next(
        issue for issue in table["review_issues"]
        if issue["code"] == "ocr_text_line_recovered"
    )
    assert issue["raw_ocr_value"] == "Catalog reference 803"
    assert issue["recovered_line"] == "Replacement wrench assembly 4 size 17mm"
    assert any(35 <= bbox[0] < 50 and 430 <= bbox[2] < 440 for bbox in captured)
    assert any(bbox[2] - bbox[0] < 100 for bbox in captured)
    assert all(bbox[1] < row_boxes[missing_row][1] + 3 for bbox in captured)
    assert all(bbox[3] < row_boxes[missing_row][3] for bbox in captured)
    numeric_issue = next(
        issue for issue in table["review_issues"]
        if issue["code"] == "ocr_numeric_cell_recovered"
    )
    assert numeric_issue["recovered_value"] == "803"


def test_missing_first_text_line_low_confidence_keeps_existing_text_and_flags_review(monkeypatch):
    rows, row_boxes, items, missing_row = _make_missing_first_description_line_case()
    original = rows[missing_row][1]
    table = {
        "column_count": 5,
        "headers": ["No.", "Material", "Reference", "Amount", "Measure"],
        "header_source_bboxes": [[18, 78, 570, 95]],
        "rows": rows,
        "row_source_bboxes": row_boxes,
    }
    page = SimpleNamespace(
        rect=SimpleNamespace(x0=0, y0=0, x1=595, y1=842),
        items=items,
        ocr_word_count=len(items),
    )
    monkeypatch.setattr(
        "backend.services.ocr_adapter.recognize_ocr_text_line_crop",
        lambda *_args: {"text": "uncertain", "confidence": 0.61},
    )

    _recover_missing_ocr_text_line_from_geometry(page, table)

    assert rows[missing_row][1] == original
    assert table["rows_need_review"][missing_row] is True
    assert table["cell_review_flags"][missing_row][1] is True
    assert any(
        issue["code"] == "possible_missing_ocr_text_line"
        for issue in table["review_issues"]
    )


def test_alphabetic_ocr_noise_is_cleared_from_numeric_dominant_lane_and_flagged():
    table = {
        "column_count": 3,
        "headers": ["No.", "Description", "Requested"],
        "rows": [
            [str(index), f"Item {index}", quantity]
            for index, quantity in enumerate(["2", "5", "5", "5", "s", "10"], start=1)
        ],
    }

    _clear_non_numeric_text_from_ocr_numeric_lanes(table)

    assert table["rows"][4][2] == ""
    assert table["rows"][4][0] == "5"
    assert table["rows"][4][1] == "Item 5"
    assert table["cell_review_flags"][4] == [False, False, True]
    issue = next(
        issue for issue in table["review_issues"]
        if issue["code"] == "nonnumeric_text_cleared_from_numeric_lane"
    )
    assert issue["row_index"] == 4
    assert issue["column_index"] == 2
    assert issue["raw_ocr_value"] == "s"
    assert issue["value_cleared"] is True
    assert table["review_required"] is True


class _SlantedOCRTablePage:
    rect = SimpleNamespace(width=595.0, height=842.0, x0=0.0, y0=0.0, x1=595.0, y1=842.0)
    number = 0
    ocr_word_count = 120
    ocr_elapsed_seconds = 0.01

    def __init__(self) -> None:
        self.words = []
        for text, x0, x1 in [
            ("Record", 90, 104), ("No.", 105, 114), ("Product", 120, 144),
            ("Weight", 250, 280), ("Unit", 300, 321), ("Pack", 340, 362), ("Count", 390, 420),
        ]:
            self._add(text, x0, 180, x1, 190)

        for index in range(1, 19):
            y0 = 192 + (index - 1) * 17
            shift = 0.04 * (y0 + 5 - 185)
            values = [
                (str(index), 100, 108),
                (f"Valve assembly {index}", 120, 218),
                (str(index + 2), 250, 268),
                ("SET", 300, 321),
                ("BOX", 340, 362),
                (str(index + 11), 390, 410),
            ]
            for text, x0, x1 in values:
                self._add(text, x0 + shift, y0, x1 + shift, y0 + 10)

    def _add(self, text, x0, y0, x1, y1):
        index = len(self.words)
        self.words.append((x0, y0, x1, y1, text, 0, index, index))

    def get_text(self, option="text", *args, **kwargs):
        if option == "words":
            return self.words
        if option == "dict":
            return {
                "width": self.rect.width,
                "height": self.rect.height,
                "blocks": [{
                    "type": 0,
                    "bbox": list(word[:4]),
                    "lines": [{"spans": [{"text": word[4], "bbox": list(word[:4])}]}],
                } for word in self.words],
            }
        if option == "text":
            return "\n".join(word[4] for word in self.words)
        return ""

    def find_tables(self, *args, **kwargs):
        return SimpleNamespace(tables=[])
