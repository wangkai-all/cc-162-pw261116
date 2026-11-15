"""Catalog web application: JSON API and static file hosting.

A single :class:`http.server.ThreadingHTTPServer` serves both the API under
``/api/*`` and the static UI from ``dlcatalog/web``.  Harvest runs execute
on a background worker thread; only one run at a time is allowed and the run
state lives in the SQLite database, so an API restart resumes cleanly.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..export import build_export, export_json
from ..harvester import Harvester, MODE_CRAWL, MODE_REFETCH
from ..store import Store

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class HarvestCoordinator:
    """Owns the harvester and serialises background runs."""

    def __init__(self, store: Store):
        self.store = store
        self.harvester = Harvester(store)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, seed_url: str, mode: str) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                return {"ok": False, "error": "a harvest run is already active"}
            thread = threading.Thread(
                target=self._run, args=(seed_url, mode), daemon=True,
                name=f"harvest-{mode}")
            self._thread = thread
            thread.start()
            return {"ok": True, "mode": mode, "seed": seed_url}

    def refetch_node(self, url: str) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                return {"ok": False, "error": "a harvest run is already active"}
            thread = threading.Thread(
                target=self._run_refetch, args=(url,), daemon=True,
                name="harvest-refetch-node")
            self._thread = thread
            thread.start()
            return {"ok": True, "mode": "refetch-node", "seed": url}

    def _run(self, seed_url: str, mode: str) -> None:
        try:
            self.harvester.run(seed_url, mode=mode)
        except Exception as exc:  # pragma: no cover - recorded in run row
            print(f"harvest run failed: {exc}")

    def _run_refetch(self, url: str) -> None:
        try:
            self.harvester.refetch_node(url)
        except Exception as exc:  # pragma: no cover
            print(f"node refetch failed: {exc}")

    def status(self) -> dict[str, Any]:
        run_row = self.store.running_run()
        active = self.is_running()
        return {
            "active": active,
            "progress": self.harvester.progress() if active else {},
            "running_run": dict(run_row) if run_row else None,
            "latest_run": dict(self.store.latest_run())
            if self.store.latest_run() else None,
            "counts": self.store.count_by_state(),
        }


class CatalogHandler(BaseHTTPRequestHandler):
    server_version = "dlcatalog/1.0"

    # Injected by make_app:
    store: Store
    coordinator: HarvestCoordinator
    export_dir: str

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- helpers

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    # ----------------------------------------------------------------- GET

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route.startswith("/api/"):
            self._handle_api_get(route)
            return
        self._serve_static(route)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if not route.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        body = self._read_body()
        if route == "/api/harvest/start":
            seed = (body.get("seed_url") or "").strip()
            mode = body.get("mode") if body.get("mode") in (
                MODE_CRAWL, MODE_REFETCH) else MODE_CRAWL
            if not seed:
                self._json({"ok": False, "error": "seed_url is required"}, 400)
                return
            self._json(self.coordinator.start(seed, mode))
            return
        if route == "/api/harvest/refetch":
            url = (body.get("url") or "").strip()
            if not url:
                self._json({"ok": False, "error": "url is required"}, 400)
                return
            self._json(self.coordinator.refetch_node(url))
            return
        self._json({"error": "not found"}, 404)

    def _handle_api_get(self, route: str) -> None:
        query = self._query()
        store = self.store

        if route == "/api/status":
            self._json(self.coordinator.status())
            return

        if route == "/api/nodes":
            state = query.get("state", [None])[0]
            node_type = query.get("type", [None])[0]
            rows = store.list_nodes(state=state, node_type=node_type)
            self._json([store.node_to_dict(row) for row in rows])
            return

        if route == "/api/graph":
            self._json(self._graph_payload())
            return

        if route == "/api/changes":
            run_id = query.get("run_id", [None])[0]
            kind = query.get("kind", [None])[0]
            rows = store.list_changes(
                run_id=int(run_id) if run_id else None,
                change_kind=kind)
            self._json([dict(row) for row in rows])
            return

        if route == "/api/runs":
            self._json([dict(row) for row in store.list_runs()])
            return

        if route.startswith("/api/nodes/"):
            url = route[len("/api/nodes/"):]
            # URLs arrive percent-encoded; BaseHTTPRequestHandler gives the
            # raw path, so decode it explicitly.
            from urllib.parse import unquote
            url = unquote(url)
            self._node_detail(url)
            return

        if route == "/api/export":
            seed = query.get("seed", [None])[0]
            if "download" in query:
                payload = build_export(store, seed)
                self._json(payload)
                return
            path = export_json(store, self.export_dir, seed)
            self._json({"ok": True, "path": path})
            return

        self._json({"error": "not found"}, 404)

    def _node_detail(self, url: str) -> None:
        store = self.store
        row = store.get_node(url)
        if row is None:
            self._json({"error": "unknown node", "url": url}, 404)
            return
        detail = store.node_to_dict(row)
        detail["parents"] = [store.node_to_dict(p)
                             for p in store.get_parents(url)]
        detail["children"] = [store.node_to_dict(c)
                              for c in store.get_children(url)]
        detail["source_paths"] = store.source_paths().get(url, [])
        if row["node_type"] != "collection":
            detail["images"] = [img.to_dict()
                                for img in store.images_for(url)]
        else:
            detail["members"] = [m.to_dict() for m in store.members_for(url)]
        self._json(detail)

    def _graph_payload(self) -> dict:
        store = self.store
        nodes = [store.node_to_dict(row) for row in store.list_nodes()]
        edges = [{"parent": parent, "child": child}
                 for parent, child in store.all_edges()]
        roots = store.root_nodes()
        return {"nodes": nodes, "edges": edges, "roots": roots}

    # ------------------------------------------------------------ static UI

    def _serve_static(self, route: str) -> None:
        if route in ("", "/"):
            route = "/index.html"
        # Prevent path traversal: only allow simple relative file names.
        safe = route.lstrip("/").replace("..", "")
        target = os.path.normpath(os.path.join(WEB_ROOT, safe))
        if not target.startswith(WEB_ROOT) or not os.path.isfile(target):
            self.send_error(404)
            return
        ext = os.path.splitext(target)[1]
        with open(target, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_app(store: Store, export_dir: str, host: str = "127.0.0.1",
             port: int = 8000) -> ThreadingHTTPServer:
    coordinator = HarvestCoordinator(store)

    class _Handler(CatalogHandler):
        pass

    _Handler.store = store
    _Handler.coordinator = coordinator
    _Handler.export_dir = export_dir
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.coordinator = coordinator  # type: ignore[attr-defined]
    return httpd


def serve(db_path: str, export_dir: str, host: str = "127.0.0.1",
          port: int = 8000, verbose: bool = False) -> None:
    store = Store(db_path)
    httpd = make_app(store, export_dir, host, port)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    print(f"catalog web UI: http://{host}:{port}  (db={db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()
