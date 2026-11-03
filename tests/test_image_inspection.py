"""Regression coverage for image metadata on PDFs using tiling patterns."""

import pymupdf

from backend.services.document_parser import (
    _page_image_coverage,
    _page_image_info_for_classification,
)
from backend.services.page_classifier import classify_page_source


def _pdf_with_tiling_pattern_and_shading() -> bytes:
    """Build a tiny PDF containing a tiling pattern and nested axial shading."""
    document = pymupdf.open()
    page = document.new_page(width=200, height=200)

    def add_object(dictionary: str, stream: bytes | None = None) -> int:
        xref = document.get_new_xref()
        document.update_object(xref, dictionary)
        if stream is not None:
            document.update_stream(xref, stream)
        return xref

    function = add_object(
        "<< /FunctionType 2 /Domain [0 1] /C0 [1 1 1] /C1 [0.8 0.8 0.8] /N 1 >>"
    )
    shading = add_object(
        f"<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 6 6] "
        f"/Function {function} 0 R /Extend [true true] >>"
    )
    form_resources = add_object(f"<< /Shading << /S {shading} 0 R >> >>")
    form = add_object(
        f"<< /Type /XObject /Subtype /Form /BBox [0 0 6 6] "
        f"/Resources {form_resources} 0 R >>",
        b"q 0 0 6 6 re W n /S sh Q",
    )
    pattern_resources = add_object(f"<< /XObject << /X {form} 0 R >> >>")
    pattern = add_object(
        f"<< /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 6 6] "
        f"/XStep 6 /YStep 6 /Resources {pattern_resources} 0 R >>",
        b"/X Do",
    )
    page_resources = add_object(f"<< /Pattern << /P {pattern} 0 R >> >>")
    page_content = add_object(
        "<< >>",
        b"/Pattern cs /P scn 0 0 200 200 re f",
    )
    document.xref_set_key(page.xref, "Resources", f"{page_resources} 0 R")
    document.xref_set_key(page.xref, "Contents", f"{page_content} 0 R")

    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


def _pdf_with_inline_scan_and_hidden_text() -> bytes:
    """Build a page with a full-page inline raster and a small text layer."""
    document = pymupdf.open()
    page = document.new_page(width=200, height=200)
    page.insert_text((20, 24), "hidden OCR text", fontsize=8)
    contents = list(page.get_contents())
    inline_image = document.get_new_xref()
    document.update_object(inline_image, "<< >>")
    document.update_stream(
        inline_image,
        b"q 180 0 0 180 10 10 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00 EI Q",
    )
    contents.append(inline_image)
    document.xref_set_key(
        page.xref,
        "Contents",
        "[" + " ".join(f"{xref} 0 R" for xref in contents) + "]",
    )
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


def test_xrefs_option_preserves_tiling_pattern_geometry_and_classification():
    document = pymupdf.open(stream=_pdf_with_tiling_pattern_and_shading(), filetype="pdf")
    try:
        page = document[0]
        with_xrefs = page.get_image_info(xrefs=True)
        without_xrefs = page.get_image_info(xrefs=False)

        assert len(with_xrefs) == len(without_xrefs) > 0
        assert [item["bbox"] for item in with_xrefs] == [
            item["bbox"] for item in without_xrefs
        ]
        assert all(item["xref"] == 0 for item in with_xrefs)
        assert all(item.get("xref") in (None, 0) for item in without_xrefs)

        coverage_with_xrefs = _page_image_coverage(page, len(with_xrefs), with_xrefs)
        coverage_without_xrefs = _page_image_coverage(
            page, len(without_xrefs), without_xrefs
        )
        assert coverage_with_xrefs == coverage_without_xrefs

        assert classify_page_source(
            has_extractable_text=True,
            image_count=len(with_xrefs),
            image_coverage=coverage_with_xrefs,
        ) == classify_page_source(
            has_extractable_text=True,
            image_count=len(without_xrefs),
            image_coverage=coverage_without_xrefs,
        )
    finally:
        document.close()


def test_vector_tiling_pattern_skips_expensive_image_info_expansion():
    document = pymupdf.open(
        stream=_pdf_with_tiling_pattern_and_shading(), filetype="pdf"
    )
    try:
        page = document[0]
        assert page.get_images(full=True) == []
        image_count, image_info = _page_image_info_for_classification(page)
        assert image_count == 0
        assert image_info == []
        assert classify_page_source(
            has_extractable_text=True,
            image_count=image_count,
            image_coverage=_page_image_coverage(page, image_count, image_info),
        ) == "digital_candidate"
    finally:
        document.close()


def test_inline_scan_with_text_layer_keeps_bbox_coverage_for_classification():
    document = pymupdf.open(
        stream=_pdf_with_inline_scan_and_hidden_text(), filetype="pdf"
    )
    try:
        page = document[0]
        assert page.get_images(full=True) == []
        image_count, image_info = _page_image_info_for_classification(page)
        coverage = _page_image_coverage(page, image_count, image_info)

        assert image_info
        assert image_count > 0
        assert coverage >= 0.80
        assert classify_page_source(
            has_extractable_text=True,
            image_count=image_count,
            image_coverage=coverage,
        ) == "scan_with_text_layer"
    finally:
        document.close()


def test_raster_page_keeps_bbox_coverage_for_scan_classification():
    document = pymupdf.open()
    try:
        page = document.new_page(width=200, height=200)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200))
        pixmap.clear_with(255)
        page.insert_image(page.rect, pixmap=pixmap)

        image_count, image_info = _page_image_info_for_classification(page)
        coverage = _page_image_coverage(page, image_count, image_info)

        assert image_count > 0
        assert image_info
        assert coverage >= 0.80
        assert classify_page_source(
            has_extractable_text=False,
            image_count=image_count,
            image_coverage=coverage,
        ) == "scan_candidate"
    finally:
        document.close()


def test_exact_duplicate_image_bboxes_do_not_change_coverage():
    document = pymupdf.open()
    try:
        page = document.new_page(width=200, height=200)
        bbox = (10.0, 20.0, 80.0, 120.0)
        entries = [{"bbox": bbox}, {"bbox": bbox}, {"bbox": (60.0, 20.0, 100.0, 120.0)}]

        duplicated_coverage = _page_image_coverage(page, len(entries), entries)
        unique_coverage = _page_image_coverage(
            page,
            2,
            [{"bbox": bbox}, {"bbox": (60.0, 20.0, 100.0, 120.0)}],
        )

        assert duplicated_coverage == unique_coverage
    finally:
        document.close()
