"""Recursive IIIF collection harvester.

Cycle handling
--------------
A crawl is a graph traversal, not a tree walk.  A run-level ``visited`` set
guarantees each URL is fetched at most once per run, so mutual collection
references terminate.  Membership is recorded separately as parent/child
*edges* before the visited check, so a manifest appearing in several
collections keeps every collection path even though its document is only
downloaded once.

Resumption
----------
Fetched nodes are committed immediately in state ``confirmed`` (or
``failed``); merely discovered nodes are ``pending``.  A new run starts from
the pending set, which makes interruption and restart safe without any
checkpoint file.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from typing import Callable
from urllib.parse import urldefrag

from .diff import diff_nodes
from .httpclient import FetchResult, fetch
from .iiif import parse_document
from .store import RUN_DONE, RUN_FAILED, Store

# Harvest modes stored on the run row.
MODE_CRAWL = "crawl"            # seed + pending frontier, resume-safe
MODE_REFETCH = "refetch"        # re-fetch every reachable node (revision check)
MODE_REFETCH_NODE = "refetch-node"

Fetcher = Callable[..., FetchResult]


def normalize_url(url: str) -> str:
    """Drop the fragment; the query string is significant and is retained."""
    return urldefrag(url)[0]


class Harvester:
    def __init__(self, store: Store, fetcher: Fetcher = fetch,
                 timeout: float = 20.0):
        self.store = store
        self._fetch = fetcher
        self.timeout = timeout
        self._progress_lock = threading.Lock()
        self._progress: dict = {}

    # ------------------------------------------------------------- progress

    def progress(self) -> dict:
        with self._progress_lock:
            return dict(self._progress)

    def _set_progress(self, **values) -> None:
        with self._progress_lock:
            self._progress.update(values)

    def _reachable_from(self, seed_url: str) -> list[str]:
        """BFS over stored edges, cycle-safe, in discovery order."""
        seen = {seed_url}
        ordered: list[str] = [seed_url]
        frontier = deque([seed_url])
        while frontier:
            current = frontier.popleft()
            for child in self.store.get_children(current):
                if child["url"] not in seen:
                    seen.add(child["url"])
                    ordered.append(child["url"])
                    frontier.append(child["url"])
        return ordered

    # --------------------------------------------------------------- fetch

    def _fetch_url(self, url: str) -> FetchResult:
        row = self.store.get_node(url)
        etag = row["etag"] if row is not None else None
        last_modified = row["last_modified"] if row is not None else None
        return self._fetch(url, timeout=self.timeout,
                           etag=etag, last_modified=last_modified)

    def _process_node(self, url: str, run_id: int,
                      visited: set[str]) -> None:
        """Fetch and integrate one node.  Never raises."""
        result = self._fetch_url(url)
        if not result.ok:
            self.store.save_failed(url, result.status, result.error)
            self.store.update_run(run_id, failed=1)
            return

        # 304 Not Modified: stored document bytes are still accurate, but
        # refresh the validator headers the server echoed.
        if result.status == 304:
            self.store.mark_unmodified(url, etag=result.etag,
                                       last_modified=result.last_modified)
            return

        try:
            doc = json.loads(result.text)
        except (json.JSONDecodeError, ValueError) as exc:
            self.store.save_failed(url, result.status,
                                   f"invalid JSON: {exc}")
            self.store.update_run(run_id, failed=1)
            return
        if not isinstance(doc, dict):
            self.store.save_failed(url, result.status,
                                   "JSON document is not an object")
            self.store.update_run(run_id, failed=1)
            return

        new_parsed = parse_document(doc, url)
        old_parsed = self.store.parsed_node(url)
        self.store.save_fetched(
            new_parsed, result.status or 200,
            result.etag, result.last_modified)

        if old_parsed is not None:
            changes = diff_nodes(old_parsed, new_parsed)
            if changes:
                self.store.record_changes(run_id, changes)
                self.store.update_run(run_id, changed=len(changes))

        # Integrate children: edges first (membership preservation), then
        # schedule the documents that this run has not fetched yet.  The
        # visited check happens at dequeue time, so nothing is silently
        # skipped just because it was enqueued early.
        child_urls = [normalize_url(m.url) for m in new_parsed.members]
        self.store.replace_edges(url, child_urls, run_id)
        for member in new_parsed.members:
            child_url = normalize_url(member.url)
            self.store.upsert_discovered(child_url, member.type_hint)
            if child_url not in visited:
                yield child_url  # type: ignore[misc]

    # ----------------------------------------------------------------- runs

    def run(self, seed_url: str, mode: str = MODE_CRAWL) -> int:
        """Execute a harvest run and return its run id."""
        seed_url = normalize_url(seed_url)
        run_id = self.store.create_run(seed_url, mode)
        visited: set[str] = set()
        queue: deque[str] = deque()

        try:
            self.store.upsert_discovered(seed_url, "collection")
            if mode == MODE_REFETCH:
                # Revision check: re-walk every node reachable from the seed
                # through the stored edges, regardless of its current state.
                # The visited set inside the loop still guarantees one fetch
                # per URL even when the graph contains cycles.
                reachable = self._reachable_from(seed_url)
                queue.extend(reachable)
            else:
                # Resume semantics: retry the seed, every still-pending node
                # and every node whose last attempt failed.  Confirmed nodes
                # are left untouched in crawl mode; refetch mode revisits
                # them explicitly for revision checks.
                queue.append(seed_url)
                for pending in self.store.list_pending():
                    queue.append(normalize_url(pending))
                for failed in self.store.list_failed():
                    queue.append(normalize_url(failed["url"]))

            self._set_progress(run_id=run_id, mode=mode, seed=seed_url,
                               queued=len(queue), fetched=0, failed=0,
                               current=None, status="running")

            while queue:
                url = normalize_url(queue.popleft())
                if url in visited:
                    continue
                visited.add(url)
                self._set_progress(current=url, queued=len(queue))
                for child in self._process_node(url, run_id, visited):
                    queue.append(child)
                self.store.update_run(run_id, fetched=1)
                self._bump_progress()

            self.store.finish_run(run_id, RUN_DONE)
            self._set_progress(status="done", current=None)
            return run_id
        except Exception as exc:  # pragma: no cover - defensive
            self.store.finish_run(run_id, RUN_FAILED, str(exc))
            self._set_progress(status="failed", error=str(exc))
            raise

    def refetch_node(self, url: str) -> int:
        """Re-fetch a single node and crawl anything new it introduces."""
        url = normalize_url(url)
        if self.store.get_node(url) is None:
            self.store.upsert_discovered(url, None)
        run_id = self.store.create_run(url, MODE_REFETCH_NODE)
        visited: set[str] = set()
        queue: deque[str] = deque([url])
        try:
            self._set_progress(run_id=run_id, mode=MODE_REFETCH_NODE,
                               seed=url, queued=1, fetched=0, failed=0,
                               current=None, status="running")
            while queue:
                current = normalize_url(queue.popleft())
                if current in visited:
                    continue
                visited.add(current)
                self._set_progress(current=current, queued=len(queue))
                for child in self._process_node(current, run_id, visited):
                    queue.append(child)
                self.store.update_run(run_id, fetched=1)
                self._bump_progress()
            self.store.finish_run(run_id, RUN_DONE)
            self._set_progress(status="done", current=None)
            return run_id
        except Exception as exc:  # pragma: no cover - defensive
            self.store.finish_run(run_id, RUN_FAILED, str(exc))
            self._set_progress(status="failed", error=str(exc))
            raise

    def _bump_progress(self) -> None:
        run = self.store.latest_run()
        if run is not None:
            self._set_progress(fetched=run["fetched"], failed=run["failed"],
                               changed=run["changed"])
