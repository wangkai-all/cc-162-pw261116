"""Local fixture HTTP server.

Serves the generated IIIF documents and exposes a small control API that
drives the three failure/change scenarios without editing files:

* transient failures (a counter persisted in the state file)
* permanent failures
* manifest revisions ("revised" variant served in place of the original)

The state file makes the scenario survive a server restart.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

STATE_FILE = "server-state.json"


class FixtureState:
    """Mutable, persisted server control state."""

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, STATE_FILE)
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {
            "flaky_remaining": {},   # doc path -> remaining failure count
            "revisions": {},         # doc path -> revision relative path
            "request_log": [],       # recent requests for diagnostics
        }
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as handle:
                    stored = json.load(handle)
                self.data.update(stored)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def manifest(self) -> dict:
        with open(os.path.join(self.root, "fixture-manifest.json"),
                  encoding="utf-8") as handle:
            return json.load(handle)


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "dlcatalog-fixture/1.0"

    # Injected by make_server:
    state: FixtureState

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ responses

    def _send_json(self, payload: Any, status: int = 200,
                   headers: Optional[dict] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, status: int = 200,
                    content_type: str = "application/json; charset=utf-8",
                    headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, message: str) -> None:
        self._send_json({"error": message}, status=404)

    # ---------------------------------------------------------------- routing

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.lstrip("/")

        if path == "__control/state":
            with self.state.lock:
                self._send_json(self.state.data)
            return
        if path == "__control/manifest":
            self._send_json(self.state.manifest())
            return
        if path == "__control/reset":
            self._handle_reset()
            return

        with self.state.lock:
            self.state.data["request_log"].append(path)
            self.state.data["request_log"] = \
                self.state.data["request_log"][-100:]
            self.state.save()
            resolved = self._resolve(path)
        if resolved is None:
            self._not_found(f"no fixture document at /{path}")
            return

        file_path, failure = resolved
        if failure is not None:
            self._send_json({"error": failure}, status=503)
            return
        if not os.path.isfile(file_path):
            self._not_found(f"missing fixture document /{path}")
            return

        with open(file_path, "rb") as handle:
            body = handle.read()
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self._send_bytes(body, headers={"ETag": etag})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/__control":
            self._not_found("unknown control endpoint")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return

        action = payload.get("action")
        with self.state.lock:
            if action == "set_flaky":
                doc = payload["path"]
                self.state.data["flaky_remaining"][doc] = int(
                    payload.get("fail_times", 1))
            elif action == "set_revision":
                doc = payload["path"]
                revision = payload.get("revision")
                if revision:
                    self.state.data["revisions"][doc] = revision
                else:
                    self.state.data["revisions"].pop(doc, None)
            elif action == "reset_state":
                self.state.data["flaky_remaining"] = {}
                self.state.data["revisions"] = {}
            else:
                self._send_json({"error": f"unknown action {action!r}"},
                                status=400)
                return
            self.state.save()
        self._send_json({"ok": True, "state": self._public_state()})

    def _handle_reset(self) -> None:
        with self.state.lock:
            self.state.data["flaky_remaining"] = {}
            self.state.data["revisions"] = {}
            self.state.save()
        self._send_json({"ok": True})

    def _public_state(self) -> dict:
        return {
            "flaky_remaining": dict(self.state.data["flaky_remaining"]),
            "revisions": dict(self.state.data["revisions"]),
        }

    # --------------------------------------------------------- file resolution

    def _resolve(self, path: str) -> Optional[tuple[str, Optional[str]]]:
        """Map a request path to a file path, applying control state."""
        manifest = self.state.manifest()

        # Image addresses must never resolve: the harvester only stores them.
        if path.startswith("images/"):
            return None

        remaining = self.state.data["flaky_remaining"].get(path)
        if remaining and remaining > 0:
            self.state.data["flaky_remaining"][path] = remaining - 1
            self.state.save()
            return path, f"simulated transient failure for /{path}"

        if path in manifest.get("permanent_failures", []):
            return None

        revision = self.state.data["revisions"].get(path)
        if revision:
            return os.path.join(self.state.root, revision), None
        return os.path.join(self.state.root, path), None


def make_server(root: str, host: str = "127.0.0.1",
                port: int = 8090) -> ThreadingHTTPServer:
    state = FixtureState(root)

    class _Handler(FixtureHandler):
        pass

    _Handler.state = state
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.fixture_state = state  # type: ignore[attr-defined]
    return httpd


def serve(root: str, host: str = "127.0.0.1", port: int = 8090,
          verbose: bool = False) -> None:
    httpd = make_server(root, host, port)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    print(f"fixture server listening on http://{host}:{port} "
          f"(root={root})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
