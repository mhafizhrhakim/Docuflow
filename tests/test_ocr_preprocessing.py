import pytest


def test_deskew_corrects_only_supported_small_skew():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from backend.services.ocr_adapter import _deskew_image, _map_entries_to_original_image

    image = np.full((900, 1200, 3), 255, dtype=np.uint8)
    for y in (160, 360, 560, 760):
        cv2.line(image, (100, y), (1050, y + 37), (0, 0, 0), 3)

    corrected, matrix, angle = _deskew_image(image)

    assert matrix is not None
    assert 1.0 <= angle <= 3.0
    before = np.array([[100.0, 160.0, 1.0], [1050.0, 197.0, 1.0]])
    after = before @ matrix.T
    corrected_slope = (after[1, 1] - after[0, 1]) / (after[1, 0] - after[0, 0])
    assert abs(corrected_slope) < 0.01
    assert corrected.shape == image.shape
    entries = [{"points": after[:, :2].tolist()}]
    _map_entries_to_original_image(entries, matrix)
    assert np.allclose(entries[0]["points"], before[:, :2], atol=1.0)


def test_deskew_leaves_vertical_or_unsupported_page_unchanged():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from backend.services.ocr_adapter import _deskew_image

    image = np.full((600, 800, 3), 255, dtype=np.uint8)
    cv2.line(image, (300, 80), (300, 500), (0, 0, 0), 3)
    corrected, matrix, angle = _deskew_image(image)

    assert matrix is None
    assert angle == 0.0
    assert np.array_equal(corrected, image)


def test_colored_round_stamp_detection_is_review_only_and_ignores_plain_table():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from backend.services.ocr_adapter import _detect_colored_stamp_regions

    image = np.full((900, 700, 3), 255, dtype=np.uint8)
    for y in range(200, 700, 55):
        cv2.line(image, (80, y), (620, y), (20, 20, 20), 2)
    cv2.circle(image, (520, 660), 74, (210, 115, 35), 5)
    cv2.putText(image, "APPROVED", (462, 666), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (210, 115, 35), 2)
    success, encoded = cv2.imencode(".png", image)
    assert success

    regions = _detect_colored_stamp_regions(encoded.tobytes())

    assert regions
    assert any(box[0] > 0.55 and box[2] > 0.65 and box[1] > 0.60 for box in regions)
    plain = np.full((900, 700, 3), 255, dtype=np.uint8)
    for y in range(200, 700, 55):
        cv2.line(plain, (80, y), (620, y), (20, 20, 20), 2)
    ok, plain_encoded = cv2.imencode(".png", plain)
    assert ok
    assert _detect_colored_stamp_regions(plain_encoded.tobytes()) == []


def test_stamp_region_cache_uses_normalized_geometry_without_changing_ocr_text():
    from types import SimpleNamespace

    from backend.services.ocr_adapter import OCRPageProxy

    page = SimpleNamespace(
        rect=SimpleNamespace(x0=0, y0=0, x1=200, y1=300, width=200, height=300)
    )
    proxy = OCRPageProxy(
        page,
        [{"text": "s", "confidence": 0.42, "points": [[100, 200], [120, 200], [120, 220], [100, 220]]}],
        200,
        300,
        0.1,
        stamp_regions=[[0.4, 0.6, 0.7, 0.8]],
    )

    assert proxy.get_stamp_regions() == [[80.0, 180.0, 140.0, 240.0]]
    assert proxy.items[0]["text"] == "s"


def test_ocr_cache_round_trips_optional_stamp_geometry_and_legacy_entries(tmp_path):
    from backend.services.ocr_adapter import _read_ocr_cache, _write_ocr_cache

    entries = [{
        "text": "5",
        "confidence": 0.9,
        "points": [[10, 10], [20, 10], [20, 20], [10, 20]],
    }]
    stamped_cache = tmp_path / "stamped.json"
    _write_ocr_cache(stamped_cache, entries, 100, 200, [[0.2, 0.3, 0.4, 0.5]])
    stamped = _read_ocr_cache(stamped_cache)
    assert stamped["stamp_regions"] == [[0.2, 0.3, 0.4, 0.5]]

    legacy_cache = tmp_path / "legacy.json"
    _write_ocr_cache(legacy_cache, entries, 100, 200)
    legacy = _read_ocr_cache(legacy_cache)
    assert legacy["entries"] == entries
    assert "stamp_regions" not in legacy
