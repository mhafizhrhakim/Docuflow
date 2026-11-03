from types import SimpleNamespace

from backend.services import rapid_table_fallback


def test_rapid_table_fallback_reuses_ocr_words_and_returns_source_geometry(monkeypatch):
    observed = {}

    class FakeEngine:
        def __call__(self, image_path, *, ocr_results, batch_size):
            observed["image_exists"] = __import__("pathlib").Path(image_path).is_file()
            observed["ocr_word_count"] = len(ocr_results[0][1])
            observed["batch_size"] = batch_size
            return SimpleNamespace(
                pred_htmls=[
                    "<table><tr><th>Item</th><th>Description</th></tr>"
                    "<tr><td>1</td><td>Valve</td></tr></table>"
                ],
                cell_bboxes=[[
                    [10, 10, 30, 20], [30, 10, 80, 20],
                    [10, 20, 30, 35], [30, 20, 80, 35],
                ]],
            )

    page = SimpleNamespace(
        _ocr_entries=[{
            "text": "1", "confidence": 0.99,
            "points": [[10, 10], [15, 10], [15, 20], [10, 20]],
        }],
        _image_width=100,
        _image_height=100,
        rect=SimpleNamespace(width=50, height=50, x0=0, y0=0),
        get_table_image_bytes=lambda: b"png bytes",
    )
    monkeypatch.setattr(rapid_table_fallback, "_RAPID_TABLE_ENGINE", FakeEngine())

    result = rapid_table_fallback.extract_rapid_table_fallback(page)

    assert observed == {"image_exists": True, "ocr_word_count": 1, "batch_size": 1}
    assert result["headers"] == ["Item", "Description"]
    assert result["rows"] == [["1", "Valve"]]
    assert result["row_count"] == 1
    assert result["column_count"] == 2
    assert result["row_source_bboxes"] == [[5.0, 10.0, 40.0, 17.5]]
    assert result["rows_need_review"] == [True]


def test_rapid_table_fallback_is_disabled_for_pages_without_ocr_words():
    page = SimpleNamespace(_ocr_entries=[], get_table_image_bytes=lambda: b"")
    assert rapid_table_fallback.extract_rapid_table_fallback(page) is None
