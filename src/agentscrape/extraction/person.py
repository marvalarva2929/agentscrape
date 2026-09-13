"""The extracted-person shape shared by the HTML and vision extractors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..db.enums import RecordRole


@dataclass
class ExtractedPerson:
    full_name: str | None = None
    email: str | None = None
    role: RecordRole = RecordRole.UNKNOWN
    pgy: int | None = None
    class_of: int | None = None
    specialty_raw: str | None = None
    # Text the value was read from, used for locating it on the screenshot.
    locate_hints: list[str] = field(default_factory=list)
    field_locations: dict[str, dict[str, int]] = field(default_factory=dict)
    confidence: float = 0.5
    source_note: str | None = None

    @property
    def is_usable(self) -> bool:
        """A person needs a name or an email; anything else is a parsing artifact."""
        return bool((self.full_name and self.full_name.strip()) or self.email)

    def to_fields(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "email": self.email,
            "role": str(self.role),
            "pgy": self.pgy,
            "class_of": self.class_of,
            "specialty_raw": self.specialty_raw,
        }
