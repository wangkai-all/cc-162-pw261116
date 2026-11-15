"""Small HTTP client used by the harvester.

Only GET is needed.  Network errors are converted into structured
:class:`FetchResult` objects instead of propagating, so the harvester can
record failure nodes and keep going.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

USER_AGENT = "dlcatalog-harvester/1.0 (IIIF collection crawler)"
DEFAULT_TIMEOUT = 20.0


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: Optional[int] = None
    body: bytes = b""
    content_type: str = ""
    error: str = ""
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def fetch(url: str, timeout: float = DEFAULT_TIMEOUT,
          etag: Optional[str] = None,
          last_modified: Optional[str] = None) -> FetchResult:
    """GET ``url`` and return a :class:`FetchResult`.

    ``etag`` / ``last_modified`` enable conditional requests: a 304 response
    is reported as ``ok=True`` with an empty body and ``status=304``.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return FetchResult(
                url=url,
                ok=True,
                status=response.status,
                body=response.read(),
                content_type=response.headers.get("Content-Type", ""),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(url=url, ok=True, status=304)
        return FetchResult(
            url=url, ok=False, status=exc.code,
            error=f"HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return FetchResult(url=url, ok=False, error=f"URL error: {exc.reason}")
    except TimeoutError:
        return FetchResult(url=url, ok=False, error="request timed out")
    except OSError as exc:
        return FetchResult(url=url, ok=False, error=f"OS error: {exc}")
