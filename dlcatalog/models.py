"""Data classes shared by the parser, harvester, store and export layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Node types as normalised by the parser.
COLLECTION = "collection"
MANIFEST = "manifest"
UNKNOWN = "unknown"

# Node lifecycle states stored in the database.
PENDING = "pending"      # discovered from a parent document, never fetched
CONFIRMED = "confirmed"  # fetched and parsed successfully
FAILED = "failed"        # fetch or parse failed at least once


@dataclass
class Title:
    """A single title value with its IIIF language tag (may be ``None``)."""

    text: str
    language: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "language": self.language}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Title":
        return Title(text=data["text"], language=data.get("language"))


@dataclass
class Image:
    """Image resource: only the address and pixel dimensions are retained.

    The image bytes themselves are never downloaded or processed.
    """

    url: str
    width: Optional[int] = None
    height: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "width": self.width, "height": self.height}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Image":
        return Image(url=data["url"], width=data.get("width"),
                     height=data.get("height"))


@dataclass
class Member:
    """A reference from a collection document to a child node."""

    url: str
    type_hint: Optional[str] = None
    label: list[Title] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "type_hint": self.type_hint,
            "label": [t.to_dict() for t in self.label],
        }


@dataclass
class ParsedNode:
    """Result of parsing one collection or manifest document."""

    url: str
    node_type: str
    titles: list[Title]
    title_source: str
    members: list[Member]
    images: list[Image]
    raw: dict[str, Any]

    def effective_title(self) -> str:
        return self.titles[0].text if self.titles else "(untitled)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "node_type": self.node_type,
            "titles": [t.to_dict() for t in self.titles],
            "title_source": self.title_source,
            "members": [m.to_dict() for m in self.members],
            "images": [i.to_dict() for i in self.images],
        }
