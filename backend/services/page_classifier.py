"""Classify PDF pages using native-text and image-coverage signals.

The classifier distinguishes:
- digital_candidate: native text without significant embedded images
- mixed_candidate: native text with embedded images covering less than the scan threshold
- scan_candidate: image-dominated page without extractable text
- scan_with_text_layer: image-dominated page with potentially unreliable hidden text
- unknown_candidate: insufficient evidence to classify the page
"""

from __future__ import annotations

import math
from typing import Final


SCAN_IMAGE_COVERAGE_THRESHOLD: Final[float] = 0.80


def _normalise_image_coverage(
    image_coverage: float | None,
) -> float | None:
    """Return valid image coverage in the inclusive range 0.0 to 1.0."""
    if image_coverage is None:
        return None

    try:
        coverage = float(image_coverage)
    except (TypeError, ValueError, OverflowError):
        return None

    if not math.isfinite(coverage):
        return None

    if not 0.0 <= coverage <= 1.0:
        return None

    return coverage


def _normalise_image_count(
    image_count: int | None,
) -> int | None:
    """Return a valid non-negative image count."""
    if image_count is None:
        return None

    if isinstance(image_count, bool):
        return None

    try:
        count = int(image_count)
    except (TypeError, ValueError, OverflowError):
        return None

    if count < 0:
        return None

    return count


def classify_page_source(
    *,
    has_extractable_text: bool,
    image_count: int | None = None,
    image_coverage: float | None = None,
) -> str:
    """Classify a page without automatically trusting a hidden text layer.

    Args:
        has_extractable_text:
            Whether PyMuPDF can extract non-empty text from the page.
        image_count:
            Number of embedded images reported by PyMuPDF.
        image_coverage:
            Fraction of page area covered by images, from 0.0 to 1.0.

    Returns:
        One of the stable classification labels consumed by document_parser.py.
    """
    has_text = bool(has_extractable_text)
    count = _normalise_image_count(image_count)
    coverage = _normalise_image_coverage(image_coverage)

    # A full-page image may contain an inaccurate hidden OCR text layer.
    # Route it through OCR even when native text is present.
    if (
        coverage is not None
        and coverage >= SCAN_IMAGE_COVERAGE_THRESHOLD
    ):
        if has_text:
            return "scan_with_text_layer"
        return "scan_candidate"

    # Preserve the fallback behavior when image count is unavailable.
    if count is None:
        return "digital_candidate" if has_text else "scan_candidate"

    # Native text with no embedded images is most likely a digital PDF.
    if has_text and count == 0:
        return "digital_candidate"

    # Native text with images that do not dominate the page is mixed.
    if has_text and count > 0:
        return "mixed_candidate"

    # Image-only pages should be passed through the OCR pipeline.
    if not has_text and count > 0:
        return "scan_candidate"

    # Neither text nor embedded images provide enough evidence.
    return "unknown_candidate"