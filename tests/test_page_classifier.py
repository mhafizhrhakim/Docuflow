from backend.services.page_classifier import classify_page_source


def test_page_with_text_is_only_a_digital_candidate():
    assert classify_page_source(has_extractable_text=True) == "digital_candidate"


def test_page_without_text_is_only_a_scan_candidate():
    assert classify_page_source(has_extractable_text=False) == "scan_candidate"
