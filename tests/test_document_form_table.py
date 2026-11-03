"""Regression tests for preserving form-like table regions before item headers."""
from __future__ import annotations

from backend.services.table_extractor import _build_document_form_table


def test_ruled_document_header_is_preserved_as_a_separate_dynamic_table() -> None:
    # Mimics a common PDF structure where PyMuPDF sees a document header box
    # and line-item grid as one large ruled-table candidate.
    rows = [
        ["TO :", "MASTER & OWNER OF M.V GAS CAMELOT", "DATE : 06/07/2026", "", "", ""],
        ["C/O", "Customer address occupying several lines", "ORD BY :", "", "", ""],
        ["PORT : VIZAG INDIA", "", "PO N GF69-1037-2026-CA", "", "", ""],
        ["INV NO - ASS/26-27/0864", "", "INV NO - ASS/26-27/0864", "", "", ""],
        ["S.NO", "DESCRIPTION", "UNIT", "QTY", "RATE", "AMT"],
        ["1", "BAKING POWDER", "GM", "2", "0.47", "0.95"],
    ]
    box = lambda x0, y0, x1, y1: (float(x0), float(y0), float(x1), float(y1))
    row_bboxes = [
        box(35, 122, 582, 150),
        box(35, 150, 582, 245),
        box(35, 245, 582, 260),
        box(35, 260, 582, 292),
        box(35, 292, 582, 309),
        box(35, 309, 582, 324),
    ]
    cell_bboxes = [
        [box(35, 122, 82, 150), box(82, 122, 358, 150), box(358, 122, 582, 150), None, None, None],
        [box(35, 150, 82, 245), box(82, 150, 358, 245), box(358, 150, 582, 245), None, None, None],
        [box(35, 245, 358, 260), None, box(358, 245, 582, 260), None, None, None],
        [box(35, 260, 582, 292), None, box(358, 260, 582, 278), None, None, None],
        [box(35, 292, 82, 309), box(82, 292, 358, 309), box(358, 292, 410, 309), box(410, 292, 468, 309), box(468, 292, 525, 309), box(525, 292, 582, 309)],
        [box(35, 309, 82, 324), box(82, 309, 358, 324), box(358, 309, 410, 324), box(410, 309, 468, 324), box(468, 309, 525, 324), box(525, 309, 582, 324)],
    ]
    candidate = {
        "strategy": "lines",
        "rows": rows,
        "row_bboxes": row_bboxes,
        "cell_bboxes": cell_bboxes,
        "bbox": box(35, 122, 582, 324),
    }

    form = _build_document_form_table(candidate, header_index=4, table_index=1)

    assert form is not None
    assert form["layout_type"] == "document_form"
    assert form["table_role"] == "document_header"
    assert form["column_count"] == 3
    assert form["row_count"] == 4
    assert form["rows"][0] == ["TO :", "MASTER & OWNER OF M.V GAS CAMELOT", "DATE : 06/07/2026"]
    assert form["rows"][2] == ["PORT : VIZAG INDIA", "", "PO N GF69-1037-2026-CA"]
    # The merged invoice-number cell was duplicated by overlapping PyMuPDF
    # cell rectangles; keep the tighter source cell once, in the right column.
    assert form["rows"][3] == ["", "", "INV NO - ASS/26-27/0864"]
    assert len(form["form_rows"][3]) == 1
    assert form["form_rows"][3][0]["column"] == 2
