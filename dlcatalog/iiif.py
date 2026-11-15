"""Parser for IIIF Presentation API v2 and v3 collection/manifest documents.

The parser is deliberately tolerant: real-world documents mix versions and
omit fields.  It produces :class:`~dlcatalog.models.ParsedNode` objects and
never raises on malformed-but-JSON input — unknown documents simply become
``unknown`` nodes so the harvester can record the failure to interpret them.
"""
from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urljoin

from .models import Image, Member, ParsedNode, Title, COLLECTION, MANIFEST, UNKNOWN

_V3_COLLECTION = "Collection"
_V3_MANIFEST = "Manifest"
_V2_COLLECTION = "sc:Collection"
_V2_MANIFEST = "sc:Manifest"


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_titles(raw_label: Any) -> list[Title]:
    """Parse an IIIF label into a list of :class:`Title` with language tags.

    Handles both v3 language maps (``{"en": ["..."]}``) and v2/v1 plain
    strings or lists of strings / ``@value`` objects.
    """
    titles: list[Title] = []
    if raw_label is None:
        return titles
    if isinstance(raw_label, str):
        if raw_label.strip():
            titles.append(Title(raw_label))
        return titles
    if isinstance(raw_label, list):
        for item in raw_label:
            if isinstance(item, str):
                if item.strip():
                    titles.append(Title(item))
            elif isinstance(item, dict):
                value = item.get("@value") or item.get("value")
                if value:
                    titles.append(Title(str(value), item.get("@language")
                                        or item.get("language")))
        return titles
    if isinstance(raw_label, dict):
        # v3 language map: language -> [strings]
        if "@value" in raw_label or "value" in raw_label:
            value = raw_label.get("@value") or raw_label.get("value")
            if value:
                titles.append(Title(
                    str(value),
                    raw_label.get("@language") or raw_label.get("language")))
            return titles
        for language, values in raw_label.items():
            lang = None if language in ("@none", "none", "") else language
            for value in _as_list(values):
                if value is not None and str(value).strip():
                    titles.append(Title(str(value), lang))
    return titles


def _detect_type(doc: dict[str, Any]) -> str:
    at_type = doc.get("@type")
    v3_type = doc.get("type")
    if v3_type in (_V3_COLLECTION, _V2_COLLECTION):
        return COLLECTION
    if v3_type in (_V3_MANIFEST, _V2_MANIFEST):
        return MANIFEST
    if at_type in (_V2_COLLECTION, _V3_COLLECTION):
        return COLLECTION
    if at_type in (_V2_MANIFEST, _V3_MANIFEST):
        return MANIFEST
    # Heuristics for documents without an explicit type.
    if "members" in doc or ("collections" in doc and "sequences" not in doc):
        return COLLECTION
    if "sequences" in doc or "items" in doc and isinstance(doc.get("items"), list):
        return MANIFEST
    return UNKNOWN


def _v3_canvas_images(canvas: dict[str, Any]) -> list[Image]:
    images: list[Image] = []
    for annotation_page in _as_list(canvas.get("items")):
        if not isinstance(annotation_page, dict):
            continue
        for annotation in _as_list(annotation_page.get("items")):
            if not isinstance(annotation, dict):
                continue
            body = annotation.get("body")
            if isinstance(body, dict) and body.get("id"):
                images.append(Image(
                    url=body["id"],
                    width=body.get("width"),
                    height=body.get("height"),
                ))
    return images


def _v3_manifest_images(doc: dict[str, Any]) -> list[Image]:
    images: list[Image] = []
    for canvas in _as_list(doc.get("items")):
        if isinstance(canvas, dict):
            images.extend(_v3_canvas_images(canvas))
    # Some manifests attach thumbnail resources worth cataloguing too.
    return _dedupe_images(images)


def _v2_manifest_images(doc: dict[str, Any]) -> list[Image]:
    images: list[Image] = []
    for sequence in _as_list(doc.get("sequences")):
        if not isinstance(sequence, dict):
            continue
        for canvas in _as_list(sequence.get("canvases")):
            if not isinstance(canvas, dict):
                continue
            for image in _as_list(canvas.get("images")):
                if not isinstance(image, dict):
                    continue
                resource = image.get("resource")
                if not isinstance(resource, dict):
                    continue
                url = resource.get("@id") or resource.get("id")
                if url:
                    images.append(Image(
                        url=url,
                        width=resource.get("width"),
                        height=resource.get("height"),
                    ))
    return _dedupe_images(images)


def _dedupe_images(images: list[Image]) -> list[Image]:
    seen: set[str] = set()
    result: list[Image] = []
    for image in images:
        if image.url in seen:
            continue
        seen.add(image.url)
        result.append(image)
    return result


def _parse_v3_members(doc: dict[str, Any], base_url: str) -> list[Member]:
    members: list[Member] = []
    for item in _as_list(doc.get("items")):
        if not isinstance(item, dict):
            continue
        url = item.get("id") or item.get("@id")
        if not url:
            continue
        type_hint = {
            _V3_COLLECTION: COLLECTION,
            _V2_COLLECTION: COLLECTION,
            _V3_MANIFEST: MANIFEST,
            _V2_MANIFEST: MANIFEST,
        }.get(item.get("type") or item.get("@type"))
        members.append(Member(
            url=urljoin(base_url, url),
            type_hint=type_hint,
            label=parse_titles(item.get("label")),
        ))
    return members


def _parse_v2_members(doc: dict[str, Any], base_url: str) -> list[Member]:
    members: list[Member] = []

    def add(item: dict[str, Any], default_type: str) -> None:
        url = item.get("@id") or item.get("id")
        if not url:
            return
        at_type = item.get("@type") or item.get("type")
        type_hint = {
            _V2_COLLECTION: COLLECTION,
            _V3_COLLECTION: COLLECTION,
            _V2_MANIFEST: MANIFEST,
            _V3_MANIFEST: MANIFEST,
        }.get(at_type, default_type)
        members.append(Member(
            url=urljoin(base_url, url),
            type_hint=type_hint,
            label=parse_titles(item.get("label")),
        ))

    for item in _as_list(doc.get("collections")):
        if isinstance(item, dict):
            add(item, COLLECTION)
    for item in _as_list(doc.get("manifests")):
        if isinstance(item, dict):
            add(item, MANIFEST)
    for item in _as_list(doc.get("members")):
        if isinstance(item, dict):
            add(item, UNKNOWN)
    # De-duplicate while keeping order (a member can appear in both
    # "manifests" and "members").
    seen: set[str] = set()
    unique: list[Member] = []
    for member in members:
        if member.url in seen:
            continue
        seen.add(member.url)
        unique.append(member)
    return unique


def _fallback_title(doc: dict[str, Any], node_type: str) -> tuple[list[Title], str]:
    """Backtrack to raw fields when no label/title is available.

    Returns the title list and the name of the source field actually used,
    so the catalog can flag records whose title is a fallback.
    """
    for field_name in ("description", "summary", "@id", "id"):
        value = doc.get(field_name)
        if isinstance(value, str) and value.strip():
            return [Title(value.strip())], f"fallback:{field_name}"
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    text = entry.get("@value") or entry.get("value")
                    if text:
                        return [Title(str(text))], f"fallback:{field_name}"
    return [Title(f"unnamed {node_type}")], "fallback:synthetic"


def parse_document(doc: dict[str, Any], url: str) -> ParsedNode:
    """Parse a decoded IIIF document fetched from ``url``."""
    node_type = _detect_type(doc)
    titles = parse_titles(doc.get("label"))
    title_source = "label"
    if not titles:
        titles, title_source = _fallback_title(doc, node_type)

    members: list[Member] = []
    images: list[Image] = []
    if node_type == COLLECTION:
        if "items" in doc:
            members = _parse_v3_members(doc, url)
        else:
            members = _parse_v2_members(doc, url)
    elif node_type == MANIFEST:
        if "items" in doc:
            images = _v3_manifest_images(doc)
        if "sequences" in doc:
            images = _v2_manifest_images(doc)

    return ParsedNode(
        url=url,
        node_type=node_type,
        titles=titles,
        title_source=title_source,
        members=members,
        images=images,
        raw=copy.deepcopy(doc),
    )
