"""Regression checks for explicit extraction roles and unified metadata layout."""
from __future__ import annotations

from backend.services.table_extractor import (
    _build_document_metadata_table,
    _extract_from_native_candidates,
)


def test_page_metadata_is_one_unified_display_table_with_source_positions() -> None:
    items = [
        {"label": "Maker", "value": "FUKUI", "source_page": 1, "source_bbox": [100, 120, 200, 130]},
        {"label": "Vessel", "value": 'LPG "Gas Suasa"', "source_page": 1, "source_bbox": [100, 150, 260, 160]},
        {"label": "Customer", "value": "OSM Thome", "source_page": 1, "source_bbox": [100, 180, 230, 190]},
        # Duplicate label/value at a different y coordinate is preserved: source
        # documents can legitimately repeat the same value in separate sections.
        {"label": "Customer", "value": "OSM Thome", "source_page": 1, "source_bbox": [300, 300, 430, 310]},
    ]
    table = _build_document_metadata_table(items)

    assert table is not None
    assert table["layout_type"] == "document_metadata"
    assert table["table_role"] == "document_metadata"
    assert table["is_source_table"] is False
    assert table["column_count"] == 2
    assert table["row_count"] == 4
    assert table["rows"][:3] == [
        ["Maker", "FUKUI"],
        ["Vessel", 'LPG "Gas Suasa"'],
        ["Customer", "OSM Thome"],
    ]
    assert table["source_bbox"] == [100.0, 120.0, 430.0, 310.0]


def test_native_item_table_has_explicit_role_without_fixed_header_schema() -> None:
    box = lambda x0, y0, x1, y1: (float(x0), float(y0), float(x1), float(y1))
    rows = [
        ["Reference", "Units", "Description"],
        ["PX-17", "2", "Pressure relief assembly"],
        ["PX-18", "1", "Spare valve kit"],
    ]
    cell_bboxes = [
        [box(40, 100, 105, 115), box(110, 100, 150, 115), box(160, 100, 310, 115)],
        [box(40, 130, 105, 145), box(110, 130, 150, 145), box(160, 130, 310, 145)],
        [box(40, 152, 105, 167), box(110, 152, 150, 167), box(160, 152, 310, 167)],
    ]
    candidate = {
        "strategy": "lines",
        "rows": rows,
        "row_bboxes": [box(40, 100, 310, 115), box(40, 130, 310, 145), box(40, 152, 310, 167)],
        "cell_bboxes": cell_bboxes,
        "bbox": box(40, 100, 310, 167),
        "column_count": 3,
    }

    tables, data_bottom = _extract_from_native_candidates([candidate])

    assert data_bottom is not None
    assert len(tables) == 1
    assert tables[0]["table_role"] == "line_items"
    assert tables[0]["layout_type"] == "item_table"
    assert tables[0]["is_source_table"] is True
    assert tables[0]["headers"] == ["Reference", "Units", "Description"]
    assert tables[0]["rows"] == rows[1:]


def test_metadata_only_page_stays_separate_from_native_table_count() -> None:
    import pymupdf

    from backend.services.table_extractor import extract_tables_from_page

    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    for label_x, value_x, y, label, value in [
        (48, 180, 80, "Fabricator", "Northwind Industrial Ltd."),
        (48, 180, 104, "Asset", "Vessel 17"),
        (48, 180, 128, "Requested by", "Technical Operations"),
    ]:
        page.insert_text((label_x, y), label, fontsize=10)
        page.insert_text((value_x, y), value, fontsize=10)

    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "no_table_detected"
    assert result["source_table_count"] == 0
    assert len(result["tables"]) == 1
    assert len(result["metadata_items"]) == 3
    display_table = result["document_metadata_table"]
    assert display_table is not None
    assert result["tables"][0]["layout_type"] == "document_metadata"
    assert result["tables"][0]["is_source_table"] is False
    assert display_table["layout_type"] == "document_metadata"
    assert display_table["is_source_table"] is False
    assert display_table["row_count"] == 3


def test_sentence_fragment_is_not_misclassified_as_document_metadata() -> None:
    import pymupdf

    from backend.services.table_extractor import extract_tables_from_page

    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((24, 90), "with all applicable provisions.", fontsize=9)
    page.insert_text(
        (310, 90),
        "For full details, please refer to the complete document available at Standard Terms and",
        fontsize=9,
    )
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert result["status"] == "no_table_detected"
    assert result["tables"] == []
    assert result["metadata_items"] == []
    assert result["document_metadata_table"] is None
