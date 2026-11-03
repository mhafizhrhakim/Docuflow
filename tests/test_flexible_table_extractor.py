"""Regression tests for layout-driven, schema-independent table extraction."""
from __future__ import annotations

import pymupdf
from types import SimpleNamespace

from backend.services.table_extractor import _annotate_ocr_table_review, extract_tables_from_page


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
