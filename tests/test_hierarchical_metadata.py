"""Regression tests for source-shaped headers and schema-independent metadata."""
from __future__ import annotations

import pymupdf

from backend.services.table_extractor import extract_tables_from_page


def test_metadata_items_are_detected_without_a_known_field_vocabulary() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    # Unfamiliar metadata labels deliberately avoid invoice-specific terminology.
    pairs = [
        (48, 70, "Fabricator", 218, "Northwind Industrial Ltd."),
        (48, 91, "Asset", 218, "Vessel 17"),
        (48, 112, "Requested by", 218, "Technical Operations"),
    ]
    for label_x, y, label, value_x, value in pairs:
        page.insert_text((label_x, y), label, fontsize=10)
        page.insert_text((value_x, y), value, fontsize=10)

    # A small borderless table anchors extraction separately from the metadata.
    headers = [
        (48, 190, "Reference"),
        (112, 190, "Units"),
        (178, 190, "Component description"),
        (402, 190, "Unit charge"),
        (500, 190, "Line value"),
    ]
    values = [
        (48, 222, "PX-17"),
        (112, 222, "2"),
        (178, 222, "Pressure relief assembly"),
        (402, 222, "125.00"),
        (500, 222, "250.00"),
    ]
    for x, y, text in headers + values:
        page.insert_text((x, y), text, fontsize=10)

    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "table_found"
    metadata = {(item["label"], item["value"]) for item in result["metadata_items"]}
    assert ("Fabricator", "Northwind Industrial Ltd.") in metadata
    assert ("Asset", "Vessel 17") in metadata
    assert ("Requested by", "Technical Operations") in metadata
    assert all("metadata_items" in table for table in result["tables"])


def test_native_grouped_header_exposes_rowspan_and_colspan_for_frontend() -> None:
    document = pymupdf.open()
    page = document.new_page(width=740, height=420)
    xs = [40, 90, 250, 340, 400, 455, 510, 565, 620, 700]

    for y in [200, 250, 275, 300]:
        page.draw_line((40, y), (700, y), color=(0, 0, 0), width=0.6)
    page.draw_line((455, 225), (565, 225), color=(0, 0, 0), width=0.6)
    for x in xs:
        if x == 510:
            page.draw_line((x, 225), (x, 300), color=(0, 0, 0), width=0.6)
        else:
            page.draw_line((x, 200), (x, 300), color=(0, 0, 0), width=0.6)

    text_items = [
        (43, 229, "Item No."), (95, 229, "Name"),
        (255, 229, "Drawing no."), (343, 229, "Part no."),
        (403, 229, "Working qty"), (477, 219, "Balance"),
        (570, 229, "Req'd qty"), (625, 229, "Remarks"),
        (462, 245, "New"), (518, 245, "Used"),
        (45, 269, "1"), (95, 269, "SEAT"), (255, 269, "A-100"),
        (345, 269, "2"), (410, 269, "4"), (469, 269, "0"),
        (523, 269, "4"), (575, 269, "5"),
        (45, 294, "2"), (95, 294, "GASKET"), (255, 294, "A-101"),
        (345, 294, "3"), (410, 294, "8"), (469, 294, "1"),
        (523, 294, "7"), (575, 294, "10"),
    ]
    for x, y, text in text_items:
        page.insert_text((x, y), text, fontsize=9)

    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    table = result["tables"][0]
    parent = next(cell for cell in table["header_cells"] if cell["text"] == "Balance")
    children = [cell["text"] for cell in table["header_cells"] if cell["row"] == 1]
    assert parent["colspan"] == 2
    assert parent["rowspan"] == 1
    assert children == ["New", "Used"]
    assert table["headers"][5:7] == ["Balance / New", "Balance / Used"]
