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
