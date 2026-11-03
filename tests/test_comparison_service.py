from backend.api.routes.documents import ComparisonRequest, compare_documents
from backend.services.comparison_service import compare_extracted_documents


def _document(name, headers, rows, page=1):
    return {
        "filename": name,
        "pages": [{
            "page_number": page,
            "tables": [{
                "is_source_table": True,
                "table_role": "line_items",
                "headers": headers,
                "rows": rows,
            }],
        }],
    }


def test_exact_part_number_has_priority_and_quantity_mismatch_needs_review():
    result = compare_extracted_documents([
        _document("quote.pdf", ["Part Number", "Description", "Qty", "Unit"],
                  [["PN-44", "Valve assembly", "5", "set"]]),
        _document("po.pdf", ["Item Code", "Item Description", "Quantity", "UOM"],
                  [["PN44", "Valve assembly", "2", "set"]], page=3),
    ])

    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["match_method"] == "exact_part_number"
    assert group["status"] == "Perlu diperiksa"
    assert "Quantity atau unit berbeda/kosong antar dokumen." in group["review_reasons"]
    assert {item["filename"] for item in group["items"]} == {"quote.pdf", "po.pdf"}
    assert group["items"][1]["page_number"] == 3


def test_similar_descriptions_match_without_inventing_values():
    result = compare_extracted_documents([
        _document("quote.pdf", ["Description", "Qty", "Unit"],
                  [["Stainless steel flange assembly", "4", "PCS"]]),
        _document("invoice.pdf", ["Item Name", "Quantity", "Unit of Measure"],
                  [["Stainless flange assembly steel", "4", "pcs"]]),
    ])

    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["match_method"] == "description_similarity"
    assert group["status"] == "Cocok"
    assert all(item["part_number"] == "" for item in group["items"])


def test_low_similarity_stays_unmatched_and_is_marked_for_review():
    result = compare_extracted_documents([
        _document("a.pdf", ["Description", "Qty"], [["Carbon steel pipe", "4"]]),
        _document("b.pdf", ["Description", "Qty"], [["Office chair", "4"]]),
    ])

    assert len(result["groups"]) == 2
    assert all(group["status"] == "Perlu diperiksa" for group in result["groups"])
    assert all(group["match_method"] == "unmatched" for group in result["groups"])


def test_compare_endpoint_uses_existing_extraction_results_without_ocr():
    request = ComparisonRequest(documents=[
        _document("quote.pdf", ["Part Number", "Description"], [["P-10", "Pump"]]),
        _document("invoice.pdf", ["Part Number", "Description"], [["P10", "Pump"]]),
    ])

    response = compare_documents(request)

    assert response["items_compared"] == 2
    assert response["groups"][0]["match_method"] == "exact_part_number"
