"""Base contract for future document-specific audit rules."""
from typing import Any, Protocol


class DocumentRule(Protocol):
    name: str

    def apply(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        """Return findings without silently overwriting raw extracted values."""
        ...
