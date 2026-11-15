"""Manifest revision diffing.

When a node is re-fetched, its old and new parsed representations are
compared.  Three change kinds are reported:

* ``added``             — a child resource (or image) that only exists now
* ``removed``           — a child resource that only existed before
* ``metadata_changed``  — same resource identity, different metadata
                          (titles, type, image dimensions, ...)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import ParsedNode, COLLECTION

ADDED = "added"
REMOVED = "removed"
METADATA_CHANGED = "metadata_changed"


@dataclass
class Change:
    node_url: str
    change_kind: str
    resource_url: str
    resource_type: str
    detail: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "node_url": self.node_url,
            "change_kind": self.change_kind,
            "resource_url": self.resource_url,
            "resource_type": self.resource_type,
            "detail": self.detail,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }


def _title_signature(node: ParsedNode) -> list[list[Optional[str]]]:
    return [[t.text, t.language] for t in node.titles]


def _image_map(node: ParsedNode) -> dict[str, dict]:
    return {img.url: {"width": img.width, "height": img.height}
            for img in node.images}


def _member_map(node: ParsedNode) -> dict[str, Optional[str]]:
    return {m.url: m.type_hint for m in node.members}


def diff_nodes(old: ParsedNode, new: ParsedNode) -> list[Change]:
    """Compare two parsed versions of the same document."""
    changes: list[Change] = []
    url = new.url

    if old.node_type != new.node_type:
        changes.append(Change(
            node_url=url, change_kind=METADATA_CHANGED,
            resource_url=url, resource_type=new.node_type,
            detail="node type changed",
            old_value=old.node_type, new_value=new.node_type,
        ))
    if _title_signature(old) != _title_signature(new):
        changes.append(Change(
            node_url=url, change_kind=METADATA_CHANGED,
            resource_url=url, resource_type=new.node_type,
            detail="titles changed",
            old_value=repr(_title_signature(old)),
            new_value=repr(_title_signature(new)),
        ))
    if old.title_source != new.title_source:
        changes.append(Change(
            node_url=url, change_kind=METADATA_CHANGED,
            resource_url=url, resource_type=new.node_type,
            detail="title source changed",
            old_value=old.title_source, new_value=new.title_source,
        ))

    if new.node_type == COLLECTION or old.node_type == COLLECTION:
        old_members = _member_map(old)
        new_members = _member_map(new)
        for member_url, hint in new_members.items():
            if member_url not in old_members:
                changes.append(Change(
                    node_url=url, change_kind=ADDED,
                    resource_url=member_url,
                    resource_type=hint or "unknown",
                    detail="member added to collection",
                ))
            elif old_members[member_url] != hint:
                changes.append(Change(
                    node_url=url, change_kind=METADATA_CHANGED,
                    resource_url=member_url,
                    resource_type=hint or "unknown",
                    detail="member type hint changed",
                    old_value=old_members[member_url], new_value=hint,
                ))
        for member_url, hint in old_members.items():
            if member_url not in new_members:
                changes.append(Change(
                    node_url=url, change_kind=REMOVED,
                    resource_url=member_url,
                    resource_type=hint or "unknown",
                    detail="member removed from collection",
                ))

    old_images = _image_map(old)
    new_images = _image_map(new)
    for image_url, dims in new_images.items():
        if image_url not in old_images:
            changes.append(Change(
                node_url=url, change_kind=ADDED,
                resource_url=image_url, resource_type="image",
                detail="image added to manifest",
            ))
        elif old_images[image_url] != dims:
            changes.append(Change(
                node_url=url, change_kind=METADATA_CHANGED,
                resource_url=image_url, resource_type="image",
                detail="image dimensions changed",
                old_value=repr(old_images[image_url]),
                new_value=repr(dims),
            ))
    for image_url in old_images:
        if image_url not in new_images:
            changes.append(Change(
                node_url=url, change_kind=REMOVED,
                resource_url=image_url, resource_type="image",
                detail="image removed from manifest",
            ))

    return changes
