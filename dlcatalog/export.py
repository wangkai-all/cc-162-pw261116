"""Catalog export with provenance paths.

The export is a self-contained JSON document: every confirmed collection and
manifest gets its parsed metadata plus the *source paths* — every route from
a root collection to the node, expressed as ordered lists of node URLs.
Because the crawl graph may contain cycles and shared references, a node can
legitimately have several source paths.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .models import COLLECTION, CONFIRMED
from .store import Store


def build_export(store: Store, seed_url: Optional[str] = None) -> dict[str, Any]:
    paths = store.source_paths(seed_url)
    nodes_payload: list[dict[str, Any]] = []

    for row in store.list_nodes():
        if row["state"] != CONFIRMED:
            # Failed/pending nodes are listed separately, never as catalog data.
            continue
        record = store.node_to_dict(row)
        record["source_paths"] = paths.get(row["url"], [])
        record["parent_urls"] = [
            parent["url"] for parent in store.get_parents(row["url"])]
        if row["node_type"] == COLLECTION:
            record["members"] = [
                {"url": member.url,
                 "type_hint": member.type_hint,
                 "label": [title.to_dict() for title in member.label]}
                for member in store.members_for(row["url"])]
            record["images"] = []
        else:
            record["members"] = []
            record["images"] = [
                image.to_dict() for image in store.images_for(row["url"])]
        nodes_payload.append(record)

    failed = [store.node_to_dict(row) for row in store.list_failed()]
    runs = [dict(run) for run in store.list_runs(limit=100)]

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed_url": seed_url,
        "summary": {
            "total_confirmed": len(nodes_payload),
            "total_failed": len(failed),
            "collections": sum(1 for n in nodes_payload
                               if n["node_type"] == COLLECTION),
            "manifests": sum(1 for n in nodes_payload
                             if n["node_type"] != COLLECTION),
        },
        "nodes": nodes_payload,
        "failed_nodes": failed,
        "runs": runs,
    }


def export_json(store: Store, out_dir: str,
                seed_url: Optional[str] = None) -> str:
    """Write the export to ``out_dir`` with a timestamped filename."""
    os.makedirs(out_dir, exist_ok=True)
    payload = build_export(store, seed_url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(out_dir, f"catalog-export-{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path
