"""SQLite-backed crawl state.

The database is the durable source of truth: every discovered node, every
parent/child edge, every run and every detected change.  Writes happen in
short transactions so a process interrupted mid-crawl can resume from the
confirmed nodes on the next start (pending nodes are simply re-enqueued).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .models import (Image, Member, ParsedNode,
                     PENDING, CONFIRMED, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    url             TEXT PRIMARY KEY,
    node_type       TEXT NOT NULL,
    titles_json     TEXT NOT NULL,
    title_source    TEXT NOT NULL,
    raw_json        TEXT,
    state           TEXT NOT NULL,
    http_status     INTEGER,
    error           TEXT,
    etag            TEXT,
    last_modified   TEXT,
    first_seen_at   TEXT NOT NULL,
    last_fetched_at TEXT,
    fetch_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    parent_url    TEXT NOT NULL,
    child_url     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_run INTEGER,
    PRIMARY KEY (parent_url, child_url)
);
CREATE INDEX IF NOT EXISTS idx_edges_child ON edges(child_url);
CREATE INDEX IF NOT EXISTS idx_edges_parent ON edges(parent_url);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seed_url    TEXT NOT NULL,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL,
    node_url      TEXT NOT NULL,
    change_kind   TEXT NOT NULL,
    resource_url  TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    detail        TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (run_id, node_url, change_kind, resource_url, detail)
);
CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
CREATE INDEX IF NOT EXISTS idx_changes_node ON changes(node_url);
"""

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"

# Safety limits for path enumeration on cyclic graphs.
MAX_PATHS_PER_ROOT = 1000
MAX_TOTAL_PATHS = 10000


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe wrapper around a single SQLite database."""

    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False: the web server's API threads and the
        # background harvester share the store; every public method takes
        # the lock and keeps its transaction short.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ nodes

    def get_node(self, url: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM nodes WHERE url = ?", (url,))
            return cur.fetchone()

    def list_nodes(self, state: Optional[str] = None,
                   node_type: Optional[str] = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM nodes WHERE 1=1"
        params: list[Any] = []
        if state:
            sql += " AND state = ?"
            params.append(state)
        if node_type:
            sql += " AND node_type = ?"
            params.append(node_type)
        sql += " ORDER BY first_seen_at, url"
        with self._lock:
            return list(self._conn.execute(sql, params))

    def count_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM nodes GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    def upsert_discovered(self, url: str, type_hint: Optional[str]) -> bool:
        """Record a node reference.  Returns True if this is a new node."""
        now = utcnow()
        with self._lock:
            existing = self._conn.execute(
                "SELECT url FROM nodes WHERE url = ?", (url,)).fetchone()
            if existing:
                return False
            self._conn.execute(
                "INSERT INTO nodes(url, node_type, titles_json, "
                "title_source, state, first_seen_at) VALUES (?,?,?,?,?,?)",
                (url, type_hint or "unknown", "[]", "unknown", PENDING, now))
            self._conn.commit()
            return True

    def save_fetched(self, parsed: ParsedNode, http_status: int,
                     etag: Optional[str], last_modified: Optional[str]) -> None:
        now = utcnow()
        titles = json.dumps([t.to_dict() for t in parsed.titles],
                            ensure_ascii=False)
        raw = json.dumps(parsed.raw, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO nodes(url, node_type, titles_json, "
                "title_source, raw_json, state, http_status, etag, "
                "last_modified, first_seen_at, last_fetched_at, fetch_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(url) DO UPDATE SET node_type=excluded.node_type, "
                "titles_json=excluded.titles_json, "
                "title_source=excluded.title_source, "
                "raw_json=excluded.raw_json, state=excluded.state, "
                "http_status=excluded.http_status, etag=excluded.etag, "
                "last_modified=excluded.last_modified, "
                "last_fetched_at=excluded.last_fetched_at, "
                "fetch_count=nodes.fetch_count + 1, error=NULL",
                (parsed.url, parsed.node_type, titles, parsed.title_source,
                 raw, CONFIRMED, http_status, etag, last_modified, now, now))
            self._conn.commit()

    def save_failed(self, url: str, http_status: Optional[int],
                    error: str) -> None:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO nodes(url, node_type, titles_json, "
                "title_source, state, http_status, error, first_seen_at, "
                "last_fetched_at, fetch_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(url) DO UPDATE SET state=excluded.state, "
                "http_status=excluded.http_status, error=excluded.error, "
                "last_fetched_at=excluded.last_fetched_at, "
                "fetch_count=nodes.fetch_count + 1",
                (url, "unknown", "[]", "unknown", FAILED, http_status, error,
                 now, now))
            self._conn.commit()

    def mark_unmodified(self, url: str, etag: Optional[str] = None,
                        last_modified: Optional[str] = None) -> None:
        """Record a 304 Not Modified touch without changing the document."""
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET last_fetched_at=?, http_status=304, "
                "etag=COALESCE(?, etag), "
                "last_modified=COALESCE(?, last_modified), "
                "fetch_count=fetch_count + 1 WHERE url=?",
                (now, etag, last_modified, url))
            self._conn.commit()

    def list_pending(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT url FROM nodes WHERE state = ? "
                "ORDER BY first_seen_at, url", (PENDING,)).fetchall()
        return [row["url"] for row in rows]

    def list_failed(self) -> list[sqlite3.Row]:
        return self.list_nodes(state=FAILED)

    # ------------------------------------------------------------------ edges

    def record_edge(self, parent_url: str, child_url: str,
                    run_id: int) -> None:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO edges(parent_url, child_url, first_seen_at, "
                "last_seen_run) VALUES (?,?,?,?) "
                "ON CONFLICT(parent_url, child_url) DO UPDATE SET "
                "last_seen_run=excluded.last_seen_run",
                (parent_url, child_url, now, run_id))
            self._conn.commit()

    def replace_edges(self, parent_url: str, child_urls: list[str],
                      run_id: int) -> tuple[list[str], list[str]]:
        """Sync stored edges of ``parent_url`` with the latest manifest.

        Returns ``(added, removed)`` child URL lists.  Edges disappearing
        from the document are deleted — the child node itself is retained
        because it may still belong to another collection.
        """
        now = utcnow()
        wanted = set(child_urls)
        with self._lock:
            existing = {
                row["child_url"] for row in self._conn.execute(
                    "SELECT child_url FROM edges WHERE parent_url = ?",
                    (parent_url,)).fetchall()
            }
            removed = list(existing - wanted)
            added = list(wanted - existing)
            if removed:
                self._conn.executemany(
                    "DELETE FROM edges WHERE parent_url = ? AND child_url = ?",
                    [(parent_url, url) for url in removed])
            self._conn.executemany(
                "INSERT INTO edges(parent_url, child_url, first_seen_at, "
                "last_seen_run) VALUES (?,?,?,?) "
                "ON CONFLICT(parent_url, child_url) DO UPDATE SET "
                "last_seen_run=excluded.last_seen_run",
                [(parent_url, url, now, run_id) for url in wanted])
            self._conn.commit()
        return added, removed

    def get_children(self, parent_url: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT n.* FROM edges e JOIN nodes n "
                "ON n.url = e.child_url WHERE e.parent_url = ? "
                "ORDER BY n.url", (parent_url,)))

    def get_parents(self, child_url: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT n.* FROM edges e JOIN nodes n "
                "ON n.url = e.parent_url WHERE e.child_url = ? "
                "ORDER BY n.url", (child_url,)))

    def all_edges(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(row["parent_url"], row["child_url"])
                    for row in self._conn.execute(
                        "SELECT parent_url, child_url FROM edges")]

    def root_nodes(self) -> list[str]:
        """Nodes with no incoming edge (collection entry points)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT url FROM nodes WHERE url NOT IN "
                "(SELECT DISTINCT child_url FROM edges) "
                "ORDER BY url").fetchall()
        return [row["url"] for row in rows]

    def source_paths(self, seed_url: Optional[str] = None
                     ) -> dict[str, list[list[str]]]:
        """Enumerate every root-to-node path, cycle-safe.

        A node reachable through several collections (or several routes into
        a cyclic graph) gets one path per route.  Per-path visited sets make
        cycles terminate; global caps keep the result bounded.
        """
        paths: dict[str, list[list[str]]] = {}
        adjacency: dict[str, list[str]] = {}
        for parent, child in self.all_edges():
            adjacency.setdefault(parent, []).append(child)

        roots = [seed_url] if seed_url else self.root_nodes()
        total = 0

        def walk(node: str, path: list[str], seen: set[str]) -> None:
            nonlocal total
            if total >= MAX_TOTAL_PATHS:
                return
            bucket = paths.setdefault(node, [])
            if len(bucket) < MAX_PATHS_PER_ROOT:
                bucket.append(list(path))
                total += 1
            for child in adjacency.get(node, []):
                if child in seen or total >= MAX_TOTAL_PATHS:
                    continue
                walk(child, path + [child], seen | {child})

        for root in roots:
            if self.get_node(root) is None:
                continue
            walk(root, [root], {root})
        return paths

    # ------------------------------------------------------------------- runs

    def create_run(self, seed_url: str, mode: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs(seed_url, mode, status, started_at) "
                "VALUES (?,?,?,?)",
                (seed_url, mode, RUN_RUNNING, utcnow()))
            self._conn.commit()
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: int) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = {key} + ?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*fields.values(), run_id))
            self._conn.commit()

    def finish_run(self, run_id: int, status: str,
                   error: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
                (status, utcnow(), error, run_id))
            self._conn.commit()

    def get_run(self, run_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))

    def latest_run(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def running_run(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs WHERE status = ? "
                "ORDER BY id DESC LIMIT 1", (RUN_RUNNING,)).fetchone()

    # ---------------------------------------------------------------- changes

    def record_changes(self, run_id: int, changes: list) -> int:
        now = utcnow()
        inserted = 0
        with self._lock:
            for change in changes:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO changes(run_id, node_url, "
                    "change_kind, resource_url, resource_type, detail, "
                    "old_value, new_value, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, change.node_url, change.change_kind,
                     change.resource_url, change.resource_type, change.detail,
                     change.old_value, change.new_value, now))
                inserted += cur.rowcount or 0
            self._conn.commit()
        return inserted

    def list_changes(self, run_id: Optional[int] = None,
                     change_kind: Optional[str] = None,
                     limit: int = 500) -> list[sqlite3.Row]:
        sql = "SELECT * FROM changes WHERE 1=1"
        params: list[Any] = []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if change_kind:
            sql += " AND change_kind = ?"
            params.append(change_kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return list(self._conn.execute(sql, params))

    # ------------------------------------------------------------- serialising

    @staticmethod
    def node_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "url": row["url"],
            "node_type": row["node_type"],
            "titles": json.loads(row["titles_json"]),
            "title_source": row["title_source"],
            "state": row["state"],
            "http_status": row["http_status"],
            "error": row["error"],
            "first_seen_at": row["first_seen_at"],
            "last_fetched_at": row["last_fetched_at"],
            "fetch_count": row["fetch_count"],
        }

    @staticmethod
    def parsed_from_row(row: sqlite3.Row) -> Optional[ParsedNode]:
        if not row["raw_json"]:
            return None
        # Re-parse the stored document so members and images are populated;
        # diffing against a node with empty members would report every
        # existing child as newly added.
        from .iiif import parse_document
        return parse_document(json.loads(row["raw_json"]), row["url"])

    def parsed_node(self, url: str) -> Optional[ParsedNode]:
        row = self.get_node(url)
        if row is None:
            return None
        return self.parsed_from_row(row)

    def images_for(self, url: str) -> list[Image]:
        """Re-extract image metadata from a stored manifest document."""
        # Imported lazily to avoid a circular import at module load time.
        from .iiif import parse_document
        row = self.get_node(url)
        if row is None or not row["raw_json"]:
            return []
        return parse_document(json.loads(row["raw_json"]), url).images

    def members_for(self, url: str) -> list[Member]:
        row = self.get_node(url)
        if row is None or not row["raw_json"]:
            return []
        from .iiif import parse_document
        return parse_document(json.loads(row["raw_json"]), url).members
