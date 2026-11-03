"""Source-linked, conservative cross-document line-item reconciliation."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

try:
    from rapidfuzz import fuzz
except ImportError:  # Keep service importable until the optional runtime is installed.
    fuzz = None


_COLUMN_TERMS = {
    "part_number": ("part number", "part no", "item code", "material code", "product code", "sku", "catalog number"),
    "description": ("description", "item name", "product name", "material description", "item description"),
    "quantity": ("quantity", "qty", "q ty", "order quantity", "ordered quantity"),
    "unit": ("unit", "uom", "unit of measure"),
}


def _normalize_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _column_index(headers: list[Any], field: str) -> int | None:
    terms = _COLUMN_TERMS[field]
    normalized = [_normalize_label(header) for header in headers]
    for index, label in enumerate(normalized):
        if any(label == term or term in label for term in terms):
            return index
    return None


def _value(row: Any, index: int | None) -> str:
    if index is None or not isinstance(row, (list, tuple)) or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _normalize_part_number(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _normalize_description(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _description_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if fuzz is not None:
        return float(fuzz.token_sort_ratio(left, right))
    left_tokens = " ".join(sorted(left.split()))
    right_tokens = " ".join(sorted(right.split()))
    return SequenceMatcher(None, left_tokens, right_tokens).ratio() * 100.0


def _quantity_value(value: str) -> Decimal | None:
    """Parse common decimal/thousands separators without guessing mixed text."""
    cleaned = re.sub(r"[^0-9,.-]", "", value)
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        decimal_mark = "." if cleaned.rfind(".") > cleaned.rfind(",") else ","
        thousands_mark = "," if decimal_mark == "." else "."
        cleaned = cleaned.replace(thousands_mark, "").replace(decimal_mark, ".")
    elif "," in cleaned:
        tail = cleaned.rsplit(",", 1)[-1]
        cleaned = cleaned.replace(",", "." if len(tail) <= 2 else "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _document_items(document: dict[str, Any], document_index: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    filename = str(document.get("filename") or document.get("source_name") or f"Dokumen {document_index + 1}")
    for page in document.get("pages", []) or []:
        page_number = page.get("page_number")
        for table in page.get("tables", []) or []:
            if table.get("is_source_table") is False or table.get("table_role") == "document_header":
                continue
            headers = table.get("headers") or []
            indexes = {field: _column_index(headers, field) for field in _COLUMN_TERMS}
            for row_index, row in enumerate(table.get("rows", []) or []):
                part_number = _value(row, indexes["part_number"])
                description = _value(row, indexes["description"])
                quantity = _value(row, indexes["quantity"])
                unit = _value(row, indexes["unit"])
                if not any((part_number, description, quantity, unit)):
                    continue
                items.append({
                    "document_index": document_index,
                    "filename": filename,
                    "page_number": page_number,
                    "table_index": table.get("table_index"),
                    "row_index": row_index,
                    "part_number": part_number,
                    "description": description,
                    "quantity": quantity,
                    "unit": unit,
                    "_part_key": _normalize_part_number(part_number),
                    "_description_key": _normalize_description(description),
                })
    return items


def _quantity_mismatch(items: list[dict[str, Any]]) -> bool:
    quantities = [_quantity_value(item["quantity"]) for item in items]
    present = [value for value in quantities if value is not None]
    units = [re.sub(r"\s+", " ", item["unit"].casefold()).strip() for item in items if item["unit"]]
    return (
        len(present) != len(items)
        or len(set(present)) > 1
        or len(units) != len(items)
        or len(set(units)) > 1
    )


def compare_extracted_documents(
    documents: list[dict[str, Any]],
    *,
    description_threshold: float = 88.0,
    ambiguity_margin: float = 5.0,
) -> dict[str, Any]:
    """Match line items across extracted documents without creating values.

    Exact normalized part numbers have priority. Description similarity is used
    only when no exact part-number match exists; borderline or close competing
    candidates remain separate and are marked for review.
    """
    flattened = [item for index, document in enumerate(documents) for item in _document_items(document, index)]
    groups: list[dict[str, Any]] = []
    for item in flattened:
        eligible = [
            (group_index, group) for group_index, group in enumerate(groups)
            if all(member["document_index"] != item["document_index"] for member in group["items"])
        ]
        exact = [
            (group_index, group) for group_index, group in eligible
            if item["_part_key"] and any(
                member["_part_key"] == item["_part_key"] for member in group["items"]
            )
        ]
        match: tuple[int, dict[str, Any]] | None = exact[0] if len(exact) == 1 else None
        strategy = "exact_part_number" if match else None
        score: float | None = 100.0 if match else None
        ambiguous = len(exact) > 1

        if not exact and item["_description_key"]:
            ranked: list[tuple[float, int, dict[str, Any]]] = []
            for group_index, group in eligible:
                candidate_descriptions = [
                    member["_description_key"] for member in group["items"]
                    if member["_description_key"]
                ]
                if not candidate_descriptions:
                    continue
                similarity = max(
                    _description_similarity(item["_description_key"], candidate)
                    for candidate in candidate_descriptions
                )
                ranked.append((similarity, group_index, group))
            ranked.sort(key=lambda result: result[0], reverse=True)
            if ranked and ranked[0][0] >= description_threshold:
                close_second = len(ranked) > 1 and ranked[0][0] - ranked[1][0] < ambiguity_margin
                if close_second:
                    ambiguous = True
                else:
                    score, group_index, group = ranked[0]
                    match = (group_index, group)
                    strategy = "description_similarity"

        if match:
            target = match[1]
            target["items"].append(item)
            previous_score = target.get("similarity")
            current_score = float(score if score is not None else 0.0)
            target["similarity"] = (
                min(float(previous_score), current_score)
                if previous_score is not None else current_score
            )
            if strategy in {"exact_part_number", "description_similarity"}:
                target["match_method"] = strategy
            target["ambiguous_identity"] = target["ambiguous_identity"] or ambiguous
        else:
            groups.append({
                "items": [item],
                "similarity": score,
                "match_method": "unmatched",
                "ambiguous_identity": ambiguous,
            })

    for group in groups:
        reasons: list[str] = []
        if len(group["items"]) < 2:
            reasons.append("Tidak ditemukan pasangan yang cukup kuat di dokumen lain.")
        if group["ambiguous_identity"]:
            reasons.append("Identitas memiliki lebih dari satu kandidat yang mirip.")
        part_numbers = {
            item["_part_key"] for item in group["items"] if item["_part_key"]
        }
        if group["match_method"] == "description_similarity" and len(part_numbers) > 1:
            reasons.append("Nomor part berbeda meski deskripsinya mirip.")
        descriptions = [
            item["_description_key"] for item in group["items"]
            if item["_description_key"]
        ]
        if (
            group["match_method"] == "exact_part_number"
            and len(descriptions) > 1
            and min(
                _description_similarity(descriptions[0], description)
                for description in descriptions[1:]
            ) < 60.0
        ):
            reasons.append("Nomor part sama tetapi deskripsi sangat berbeda.")
        if len(group["items"]) > 1 and _quantity_mismatch(group["items"]):
            reasons.append("Quantity atau unit berbeda/kosong antar dokumen.")
        group["review_required"] = bool(reasons)
        group["status"] = "Perlu diperiksa" if reasons else "Cocok"
        group["review_reasons"] = reasons
        for item in group["items"]:
            item.pop("_part_key", None)
            item.pop("_description_key", None)
    return {
        "status": "completed",
        "documents_compared": len(documents),
        "items_compared": len(flattened),
        "groups": groups,
        "rapidfuzz_available": fuzz is not None,
    }


def get_comparison_status() -> dict[str, bool | str]:
    return {
        "ready": fuzz is not None,
        "message": (
            "RapidFuzz aktif; rekonsiliasi memakai nomor part eksak dan kemiripan deskripsi."
            if fuzz is not None else
            "RapidFuzz belum terpasang; fallback pembanding string standar aktif."
        ),
    }
