"""Regression tests for metadata patterns observed in real DocuFlow PDFs."""
from __future__ import annotations
from unittest.mock import patch

import pymupdf

from backend.services.table_extractor import (
    _extract_document_metadata,
    _metadata_pairs_from_runs,
    _word_runs_on_visual_baselines,
    extract_tables_from_page,
)


def _metadata_by_label(result: dict) -> dict[str, str]:
    return {item["label"]: item["value"] for item in result["metadata_items"]}


def test_multiple_explicit_label_value_pairs_on_same_baseline_are_preserved() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((32, 210), "Customer reference:", fontsize=10)
    page.insert_text((123, 210), "Gastec Tube", fontsize=10)
    page.insert_text((421, 210), "Delivery:", fontsize=10)
    page.insert_text((491, 210), "Houston", fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    metadata = _metadata_by_label(result)
    assert metadata["Customer reference"] == "Gastec Tube"
    assert metadata["Delivery"] == "Houston"


def test_currency_with_an_unlabeled_right_side_note_and_numbered_subfields() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    # One valid pair plus a separate right-side note on the same baseline.
    page.insert_text((57, 100), "Currency", fontsize=10)
    page.insert_text((157, 100), "All prices are quoted in Japanese Yen", fontsize=10)
    page.insert_text((488, 100), "ex works Singapore", fontsize=10)
    # Section heading, then two numbered label/value subfields.
    page.insert_text((57, 130), "Remarks", fontsize=10)
    page.insert_text((158, 130), "1) Maker", fontsize=10)
    page.insert_text((279, 130), "Fukui Seisakusho", fontsize=10)
    page.insert_text((158, 145), "2) Origin", fontsize=10)
    page.insert_text((279, 145), "Japan", fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    metadata = _metadata_by_label(result)
    assert metadata["Currency"] == "All prices are quoted in Japanese Yen"
    assert metadata["1) Maker"] == "Fukui Seisakusho"
    assert metadata["2) Origin"] == "Japan"
    # No source label exists for this note, so it must not be invented.
    assert "ex works Singapore" not in metadata


def test_metadata_just_above_table_header_is_not_removed_by_a_wide_margin() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((421, 220), "Delivery date:", fontsize=10)
    page.insert_text((491, 220), "13 May 2026", fontsize=10)
    # Explicitly model two separate positioned runs as produced by the source
    # PDF. (Some PDF generators merge adjacent words into a single span, so this
    # test does not depend on PyMuPDF's span grouping for this geometry case.)
    lines = [{
        "y": 216.0,
        "runs": [
            {"text": "Delivery date:", "x0": 421.0, "y0": 216.0, "x1": 476.0, "y1": 225.0, "words": []},
            {"text": "13 May 2026", "x0": 491.0, "y0": 216.0, "x1": 540.0, "y1": 225.0, "words": []},
        ],
    }]
    table = {
        "header_source_bbox": [52.0, 231.4, 560.0, 239.9],
        "row_source_bboxes": [],
    }
    try:
        with patch("backend.services.table_extractor._word_runs_on_visual_baselines", return_value=lines):
            metadata = _extract_document_metadata(page, [table], [])
    finally:
        document.close()

    pairs = {(item["label"], item["value"]) for item in metadata}
    assert ("Delivery date", "13 May 2026") in pairs


def test_metadata_parser_does_not_pair_section_heading_with_numbered_subfield() -> None:
    runs = [
        {"text": "Remarks", "x0": 57.0, "y0": 130.0, "x1": 93.0, "y1": 139.0, "words": []},
        {"text": "1) Maker", "x0": 158.0, "y0": 130.0, "x1": 194.0, "y1": 139.0, "words": []},
        {"text": "Fukui Seisakusho", "x0": 279.0, "y0": 130.0, "x1": 353.0, "y1": 139.0, "words": []},
    ]
    pairs = _metadata_pairs_from_runs(runs)
    assert any(label == "1) Maker" and value == "Fukui Seisakusho" for label, value, _ in pairs)
    assert not any(label == "Remarks" and value == "1) Maker" for label, value, _ in pairs)


def test_explicit_pair_in_single_text_run_is_not_dropped() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((50, 100), "Delivery date: 13 May 2026", fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    metadata = _metadata_by_label(result)
    assert metadata["Delivery date"] == "13 May 2026"


def test_stacked_header_fragments_are_excluded_without_hiding_nearby_metadata() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    lines = [
        {
            "y": 225.4,
            "runs": [
                {"text": "Order", "x0": 136.5, "y0": 221.2, "x1": 159.4, "y1": 229.6, "words": []},
                {"text": "Invoice", "x0": 176.0, "y0": 221.2, "x1": 204.8, "y1": 229.6, "words": []},
            ],
        },
        {
            "y": 220.6,
            "runs": [
                {"text": "Delivery date:", "x0": 421.9, "y0": 216.4, "x1": 476.2, "y1": 224.8, "words": []},
                {"text": "13 May 2026", "x0": 491.3, "y0": 216.4, "x1": 539.8, "y1": 224.8, "words": []},
            ],
        },
    ]
    table = {
        "header_source_bbox": [32.7, 231.4, 597.0, 239.9],
        "header_source_bboxes": [
            [32.7, 231.4, 597.0, 239.9],
            [136.5, 221.2, 159.4, 229.6],
            [176.0, 221.2, 204.8, 229.6],
        ],
        "row_source_bboxes": [],
    }
    try:
        with patch("backend.services.table_extractor._word_runs_on_visual_baselines", return_value=lines):
            metadata = _extract_document_metadata(page, [table], [])
    finally:
        document.close()

    pairs = {(item["label"], item["value"]) for item in metadata}
    assert ("Delivery date", "13 May 2026") in pairs
    assert not any(label == "Order" and value == "Invoice" for label, value in pairs)


def test_url_protocol_is_not_misclassified_as_document_metadata() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((50, 100), "All invoices to be uploaded via https://supplier.example/tenant", fontsize=10)
    try:
        result = extract_tables_from_page(page)
    finally:
        document.close()

    assert not any(item["label"].lower() in {"http", "https"} for item in result["metadata_items"])
