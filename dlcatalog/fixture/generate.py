"""Generate the IIIF-style fixture corpus used by the local test server.

The corpus deliberately contains every tricky case the harvester must
survive:

* a deep collection tree with multilingual (v3 language-map) titles
* a collection referenced from two parents ("diamond") and a mutual
  collection reference (A -> B -> A) so naive recursion loops forever
* a manifest without a label, exercising the title fallback chain
* a v2-style manifest (``@type`` / sequences / canvases)
* a permanently missing manifest (HTTP 404)
* a *flaky* manifest whose first requests fail, exercising retry/resume
* per-document revisions exercising added/removed/metadata changes
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

BASE = "http://127.0.0.1:{port}"


def _labels(*pairs: tuple[str, str]) -> dict[str, list[str]]:
    """Build a v3 language map from ``(language, text)`` pairs."""
    result: dict[str, list[str]] = {}
    for language, text in pairs:
        result.setdefault(language, []).append(text)
    return result


def _collection(cid: str, labels: Any, members: list[dict],
                version: int = 3) -> dict:
    if version == 2:
        return {
            "@context": "http://iiif.io/api/presentation/2/context.json",
            "@id": cid,
            "@type": "sc:Collection",
            "label": labels,
            "members": members,
        }
    return {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": cid,
        "type": "Collection",
        "label": labels,
        "items": members,
    }


def _member_ref(url: str, kind: str, label: Any) -> dict:
    return {"id": url, "type": "Manifest" if kind == "manifest"
            else "Collection", "label": label}


def _manifest(mid: str, label: Any, images: list[dict],
              summary: Optional[str] = None, version: int = 3) -> dict:
    if version == 2:
        canvases = []
        for index, image in enumerate(images):
            canvases.append({
                "@id": f"{mid}/canvas/{index + 1}",
                "@type": "sc:Canvas",
                "label": f"Canvas {index + 1}",
                "images": [{
                    "@type": "oa:Annotation",
                    "motivation": "sc:painting",
                    "resource": {
                        "@id": image["url"],
                        "@type": "dctypes:Image",
                        "width": image.get("width"),
                        "height": image.get("height"),
                    },
                }],
            })
        return {
            "@context": "http://iiif.io/api/presentation/2/context.json",
            "@id": mid,
            "@type": "sc:Manifest",
            "label": label,
            "sequences": [{
                "@id": f"{mid}/sequence/1",
                "@type": "sc:Sequence",
                "canvases": canvases,
            }],
        }

    items = []
    for index, image in enumerate(images):
        items.append({
            "id": f"{mid}/canvas/{index + 1}",
            "type": "Canvas",
            "width": image.get("width"),
            "height": image.get("height"),
            "items": [{
                "id": f"{mid}/annotation-page/{index + 1}",
                "type": "AnnotationPage",
                "items": [{
                    "id": f"{mid}/annotation/{index + 1}",
                    "type": "Annotation",
                    "motivation": "painting",
                    "body": {
                        "id": image["url"],
                        "type": "Image",
                        "width": image.get("width"),
                        "height": image.get("height"),
                    },
                }],
            }],
        })
    doc = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": mid,
        "type": "Manifest",
        "label": label,
        "items": items,
    }
    if summary:
        doc["summary"] = summary
    return doc


def _img(name: str, width: int, height: int) -> dict:
    # Image addresses only — the harvester must never fetch these.
    return {
        "url": f"{BASE.format(port=_PORT)}/images/{name}",
        "width": width,
        "height": height,
    }


_PORT = 8090  # overwritten inside generate(); module-level default for clarity


def generate(out_dir: str, port: int = 8090) -> dict[str, Any]:
    """Write every fixture document into ``out_dir``.

    Returns the manifest describing revisions and controllable failures.
    """
    base = BASE.format(port=port)
    global _PORT
    _PORT = port
    os.makedirs(out_dir, exist_ok=True)

    def c(path: str) -> str:
        return f"{base}/collections/{path}"

    def m(path: str) -> str:
        return f"{base}/manifests/{path}"

    # ----------------------------------------------------------- manifests
    asia_m = _manifest(
        m("asia.json"),
        _labels(("en", "Asian Maps"), ("zh", "亚洲地图"), ("ja", "アジアの地図")),
        [_img("asia-1.jpg", 3000, 4000), _img("asia-2.jpg", 2000, 2000)])

    europe_m = _manifest(
        m("europe.json"),
        _labels(("en", "European Atlas"), ("fr", "Atlas européen")),
        [_img("europe-1.jpg", 4096, 3072)])

    # No label at all: fallback must backtrack through summary / id.
    nolabel_m = _manifest(
        m("nolabel.json"), None,
        [_img("unnamed-1.jpg", 1024, 768)],
        summary="Uncatalogued ledger scans")

    # v2 presentation API document.
    legacy_m = _manifest(
        m("legacy-v2.json"), "Legacy Field Notes (v2)",
        [_img("legacy-1.jpg", 1600, 1200)], version=2)

    flaky_m = _manifest(
        m("flaky.json"),
        _labels(("en", "Fragile Ledger")),
        [_img("flaky-1.jpg", 800, 600)])

    shared_m = _manifest(
        m("shared.json"),
        _labels(("en", "Shared Across Collections"),
                ("de", "Sammlungsübergreifend")),
        [_img("shared-1.jpg", 2500, 2500)])

    # --------------------------------------------------------- collections
    maps_col = _collection(
        c("maps.json"),
        _labels(("en", "Historical Maps"), ("zh", "历史地图")),
        [_member_ref(m("asia.json"), "manifest",
                     _labels(("en", "Asian Maps"))),
         _member_ref(m("europe.json"), "manifest",
                     _labels(("en", "European Atlas"))),
         # Same manifest also attached to the root collection below.
         _member_ref(m("shared.json"), "manifest",
                     _labels(("en", "Shared Across Collections")))])

    archives_col = _collection(
        c("archives.json"),
        _labels(("en", "Archives"), ("fr", "Archives")),
        [_member_ref(m("legacy-v2.json"), "manifest",
                     _labels(("en", "Legacy Field Notes (v2)"))),
         _member_ref(m("nolabel.json"), "manifest", None),
         _member_ref(m("flaky.json"), "manifest",
                     _labels(("en", "Fragile Ledger"))),
         # Permanently broken reference.
         _member_ref(m("missing.json"), "manifest",
                     _labels(("en", "Lost Register"))),
         # Diamond: shared collection referenced by two parents.
         _member_ref(c("shared-col.json"), "collection",
                     _labels(("en", "Shared Collection")))])

    shared_col = _collection(
        c("shared-col.json"),
        _labels(("en", "Shared Collection")),
        [_member_ref(m("shared.json"), "manifest",
                     _labels(("en", "Shared Across Collections")))])

    # Mutual reference: special -> loopback -> special.
    loopback_col = _collection(
        c("loopback.json"),
        _labels(("en", "Loopback Shelf")),
        [_member_ref(c("special.json"), "collection",
                     _labels(("en", "Special Collections")))])

    special_col = _collection(
        c("special.json"),
        _labels(("en", "Special Collections"),
                ("es", "Colecciones especiales")),
        [_member_ref(c("loopback.json"), "collection",
                     _labels(("en", "Loopback Shelf"))),
         _member_ref(m("shared.json"), "manifest",
                     _labels(("en", "Shared Across Collections")))])

    root_col = _collection(
        c("root.json"),
        _labels(("en", "Digital Repository"),
                ("zh", "数字馆藏库"),
                ("fr", "Dépôt numérique")),
        [_member_ref(c("maps.json"), "collection",
                     _labels(("en", "Historical Maps"))),
         _member_ref(c("archives.json"), "collection",
                     _labels(("en", "Archives"))),
         _member_ref(c("special.json"), "collection",
                     _labels(("en", "Special Collections"))),
         _member_ref(m("shared.json"), "manifest",
                     _labels(("en", "Shared Across Collections")))])

    docs: dict[str, dict] = {
        "collections/root.json": root_col,
        "collections/maps.json": maps_col,
        "collections/archives.json": archives_col,
        "collections/shared-col.json": shared_col,
        "collections/special.json": special_col,
        "collections/loopback.json": loopback_col,
        "manifests/asia.json": asia_m,
        "manifests/europe.json": europe_m,
        "manifests/nolabel.json": nolabel_m,
        "manifests/legacy-v2.json": legacy_m,
        "manifests/flaky.json": flaky_m,
        "manifests/shared.json": shared_m,
    }

    # ------------------------------------------------------------- revisions
    revisions: dict[str, list[str]] = {}

    # Root revision: one member removed, one added.
    root_rev1 = json.loads(json.dumps(root_col))
    root_rev1["items"] = [
        item for item in root_rev1["items"]
        if item["id"] != c("special.json")]
    root_rev1["items"].append(_member_ref(
        c("new-shelf.json"), "collection",
        _labels(("en", "New Shelf"))))
    docs["revisions/root-r1.json"] = root_rev1
    revisions["collections/root.json"] = ["revisions/root-r1.json"]

    # Asia manifest revision: image dimensions change + a new image appears.
    asia_rev1 = _manifest(
        m("asia.json"),
        _labels(("en", "Asian Maps (revised)"), ("zh", "亚洲地图"),
                ("ja", "アジアの地図")),
        [_img("asia-1.jpg", 3200, 4200), _img("asia-2.jpg", 2000, 2000),
         _img("asia-3.jpg", 1500, 1500)])
    docs["revisions/asia-r1.json"] = asia_rev1
    revisions["manifests/asia.json"] = ["revisions/asia-r1.json"]

    # Legacy v2 revision: metadata-only change (title).
    legacy_rev1 = _manifest(
        m("legacy-v2.json"), "Legacy Field Notes — Second Edition",
        [_img("legacy-1.jpg", 1600, 1200)], version=2)
    docs["revisions/legacy-r1.json"] = legacy_rev1
    revisions["manifests/legacy-v2.json"] = ["revisions/legacy-r1.json"]

    for relative_path, document in docs.items():
        target = os.path.join(out_dir, relative_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)

    # Regeneration is a clean start: drop any persisted control state so a
    # previous run's flaky counters or revision switches cannot leak in.
    state_path = os.path.join(out_dir, "server-state.json")
    if os.path.exists(state_path):
        os.remove(state_path)

    manifest = {
        "base_url": base,
        "seed_url": c("root.json"),
        "revisions": revisions,
        "flaky": {"manifests/flaky.json": 2},  # fail first 2 requests
        "permanent_failures": ["manifests/missing.json"],
        "expected": {
            "collections": 6,
            "manifests": 6,
            "cyclic_pair": [c("special.json"), c("loopback.json")],
            "shared_manifest": m("shared.json"),
        },
    }
    with open(os.path.join(out_dir, "fixture-manifest.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest
