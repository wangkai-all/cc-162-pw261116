# Digital Collection Catalog

A web-harvesting system for **IIIF-style collection manifests**. Starting from
a local collection manifest, it recursively discovers sub-collections,
manifests and image metadata, survives reference cycles and shared
references, recovers from transient failures and restarts, and exports a
catalog where every record carries its provenance paths.

The whole project is **pure Python standard library** — no third-party
packages, no build step, no image processing.

## What it does

- **Recursive discovery** of IIIF Presentation API v2 and v3 collections and
  manifests, with relative-URL resolution.
- **Cycle-safe traversal**: mutual collection references and diamonds never
  cause infinite loops. Each URL is fetched at most once per run, while every
  parent/child *membership edge* is recorded separately, so a manifest that
  appears in several collections keeps every collection path.
- **Multilingual titles** with language tags preserved; documents without a
  label fall back through `summary` / `description` / `@id`, and the field
  actually used is recorded as the title source.
- **Image metadata only** — address, width and height. Image bytes are never
  downloaded or processed (image URLs do not even resolve on the fixture
  server).
- **Durable, resumable state** in SQLite: discovered nodes are `pending`,
  successful fetches become `confirmed`, failures become `failed`. A new run
  resumes from pending and failed nodes; confirmed nodes are never re-fetched
  in crawl mode.
- **Revision diffing**: a refetch compares old and new documents and records
  each change as `added`, `removed` or `metadata_changed` (titles, member
  lists, image dimensions, ...).
- **Single-node refetch** from the UI or API, which also crawls any children
  the revised document introduces.
- **Local fixture server** that can create reference cycles, transient
  failures, permanent 404s and manifest revisions, with its control state
  persisted across restarts.
- **Web UI** showing the discovery graph, failed nodes, harvested manifests,
  the change log and run history.
- **JSON export** with every source path (root-to-node route) attached.

## Layout

```
dlcatalog/
  models.py             data classes (Title, Image, Member, ParsedNode)
  iiif.py               tolerant IIIF v2/v3 parser
  httpclient.py         GET client; errors become structured results
  store.py              SQLite state: nodes, edges, runs, changes
  harvester.py          recursive crawler, resume, refetch, diffing
  diff.py               added / removed / metadata_changed comparison
  export.py             catalog export with provenance paths
  fixture/
    generate.py         builds the IIIF fixture corpus + revisions
    server.py           local HTTP server with a control API
  server/app.py         JSON API + static web application
  web/                  index.html, app.js, style.css (no framework)
  __main__.py           command-line entry point
tests/                  33 unit + end-to-end tests
scripts/demo.sh         full end-to-end demonstration
```

## Quick start

Requires Python 3.10+ (developed on 3.11). No dependencies to install.

```bash
# one command: generates fixtures, starts both servers, crawls, simulates a
# restart and revisions, and writes an export to ./exports
./scripts/demo.sh
```

Then open <http://127.0.0.1:8000>. The seed field is pre-filled automatically
when the fixture server is running.

### Manual commands

```bash
# 1. generate the fixture corpus (documents contain URLs for this port)
python -m dlcatalog generate-fixtures ./fixtures --port 8090

# 2. run the fixture server (control API for failures / revisions)
python -m dlcatalog fixture-server ./fixtures --port 8090

# 3. in another shell, run the catalog web application
python -m dlcatalog app --db ./runtime/catalog.db --export-dir ./exports

# CLI harvest (crawl / resume), full revision check, or single-node refetch
python -m dlcatalog harvest http://127.0.0.1:8090/collections/root.json --db ./runtime/catalog.db
python -m dlcatalog harvest <seed> --db ./runtime/catalog.db --mode refetch
python -m dlcatalog harvest <node-url> --db ./runtime/catalog.db --mode refetch-node

# export a catalog JSON document with source paths
python -m dlcatalog export --db ./runtime/catalog.db --out ./exports
```

## Fixture control API

The fixture server (default port 8090) exposes `POST /__control`:

```bash
# next N requests for a document fail with HTTP 503 (counter is persisted)
curl -X POST http://127.0.0.1:8090/__control -H 'Content-Type: application/json' \
  -d '{"action":"set_flaky","path":"manifests/flaky.json","fail_times":2}'

# serve a revised document in place of the original (or pass null to revert)
curl -X POST http://127.0.0.1:8090/__control -H 'Content-Type: application/json' \
  -d '{"action":"set_revision","path":"manifests/asia.json","revision":"revisions/asia-r1.json"}'

curl http://127.0.0.1:8090/__control/state     # inspect persisted control state
curl http://127.0.0.1:8090/__control/reset     # clear flaky counters + revisions
```

`manifests/missing.json` always returns 404. The state lives in
`fixtures/server-state.json`, so flaky counters and active revisions survive a
fixture-server restart; regenerating the corpus resets it.

### Built-in edge cases

| Fixture | Exercises |
| --- | --- |
| `special.json` ↔ `loopback.json` | mutual collection reference (cycle) |
| `shared.json` under maps, special, shared-col and root | shared membership / diamond |
| `nolabel.json` | title fallback to `summary` |
| `legacy-v2.json` | IIIF v2 (`@type`, sequences, canvases) |
| `flaky.json` | transient failure then success |
| `missing.json` | permanent 404 |
| `revisions/*-r1.json` | added/removed members, image dimension and title changes |

## Web / JSON API

| Method & path | Purpose |
| --- | --- |
| `GET /api/status` | active run, progress, node counts, latest run |
| `GET /api/graph` | all nodes, edges and roots for the discovery view |
| `GET /api/nodes?state=&type=` | list nodes, optionally filtered |
| `GET /api/nodes/<percent-encoded-url>` | node detail: parents, children, paths, images |
| `GET /api/changes?run_id=&kind=` | change log filterable by run and kind |
| `GET /api/runs` | harvest run history |
| `POST /api/harvest/start` | body `{"seed_url": "...", "mode": "crawl"|"refetch"}` |
| `POST /api/harvest/refetch` | body `{"url": "..."}` — single-node refetch |
| `GET /api/export` | write a timestamped export; `?download=1` returns it inline |

Only one harvest runs at a time; runs execute on a background thread and the
database remains the source of truth across application restarts.

## Export format

```json
{
  "exported_at": "2026-11-15T01:37:46+00:00",
  "seed_url": "http://127.0.0.1:8090/collections/root.json",
  "summary": {"total_confirmed": 12, "total_failed": 2,
              "collections": 6, "manifests": 6},
  "nodes": [
    {
      "url": ".../manifests/shared.json",
      "node_type": "manifest",
      "titles": [{"text": "Shared Across Collections", "language": "en"}],
      "title_source": "label",
      "images": [{"url": ".../images/shared-1.jpg", "width": 2500, "height": 2500}],
      "parent_urls": [".../maps.json", ".../special.json", "..."],
      "source_paths": [
        [".../root.json", ".../maps.json", ".../shared.json"],
        [".../root.json", ".../shared.json"]
      ]
    }
  ],
  "failed_nodes": [ ... ],
  "runs": [ ... ]
}
```

`source_paths` enumerates every cycle-free route from a root collection to the
node. A node reachable through several collections legitimately has several
paths; per-path visited sets and global caps keep enumeration bounded.

## Tests

```bash
python -m unittest discover -s tests -v
```

33 tests cover the parser (v2/v3, language maps, fallback), the store (edges,
diamond/cycle paths, persistence), the crawler (cycle termination, shared
membership, resume after failure, refetch diffing, single-node retry) and a
full end-to-end suite driving the real fixture server over sockets, including
a simulated process restart and persisted control state.

## Design notes

- **Graph, not tree.** Crawl state separates *node fetching* (deduplicated by
  a run-level visited set) from *membership edges* (recorded for every parent).
  That single split is what makes both cycle safety and multi-collection
  provenance straightforward.
- **Short transactions, no checkpoint file.** Each node is committed as soon
  as it is processed, so interruption only ever leaves `pending`/`failed`
  rows that the next run re-enqueues.
- **Conditional requests.** Stored `ETag` / `Last-Modified` are sent on
  refetch; a `304` touches the node without replacing its document, while a
  changed document returns `200` and is diffed against the stored version.
- **Removed memberships delete edges, not nodes.** A manifest dropped from one
  collection stays in the catalog while it belongs to another.
