#!/usr/bin/env bash
# End-to-end demonstration of the digital collection catalog.
#
# Brings up the local IIIF fixture server and the catalog web application,
# runs a crawl (with cycles, a transient failure and a permanent 404),
# simulates a restart, publishes manifest revisions and exports a catalog
# JSON document containing source paths.
#
# Usage:  ./scripts/demo.sh
set -euo pipefail

PORT_FIXTURE="${PORT_FIXTURE:-8090}"
PORT_APP="${PORT_APP:-8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FIX_DIR="$ROOT/fixtures"
DB="$ROOT/runtime/catalog.db"
EXPORT_DIR="$ROOT/exports"
rm -rf "$FIX_DIR" "$EXPORT_DIR" "$ROOT/runtime"
mkdir -p "$FIX_DIR" "$EXPORT_DIR" "$(dirname "$DB")"

cleanup() {
  echo
  echo "== shutting down =="
  [[ -n "${FIX_PID:-}" ]] && kill "$FIX_PID" 2>/dev/null || true
  [[ -n "${APP_PID:-}" ]] && kill "$APP_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "== 1. generating IIIF fixture corpus (cycles, 404, flaky, revisions) =="
python3 -m dlcatalog generate-fixtures "$FIX_DIR" --port "$PORT_FIXTURE" \
  | python3 -c "import json,sys; m=json.load(sys.stdin); print('   seed:', m['seed_url'])"
SEED="http://127.0.0.1:$PORT_FIXTURE/collections/root.json"

echo "== 2. starting fixture server and catalog web app =="
python3 -m dlcatalog fixture-server "$FIX_DIR" --port "$PORT_FIXTURE" &
FIX_PID=$!
python3 -m dlcatalog app --db "$DB" --export-dir "$EXPORT_DIR" --port "$PORT_APP" &
APP_PID=$!
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$PORT_FIXTURE/__control/manifest" >/dev/null \
    && curl -sf "http://127.0.0.1:$PORT_APP/api/status" >/dev/null && break
  sleep 0.5
done

echo "== 3. arming a transient failure (flaky manifest fails once) =="
curl -s -X POST "http://127.0.0.1:$PORT_FIXTURE/__control" \
  -H 'Content-Type: application/json' \
  -d '{"action":"set_flaky","path":"manifests/flaky.json","fail_times":1}' \
  | python3 -c "import json,sys; print('  ', json.load(sys.stdin)['state'])"

echo "== 4. first crawl from the seed =="
python3 -m dlcatalog harvest "$SEED" --db "$DB" \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print('   run', r['id'], r['status'], 'fetched:', r['fetched'], 'failed:', r['failed'])"

echo "== 5. simulating a restart: second crawl resumes from confirmed nodes =="
python3 -m dlcatalog harvest "$SEED" --db "$DB" \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print('   run', r['id'], r['status'], 'fetched:', r['fetched'], 'failed:', r['failed'])"

echo "== 6. publishing manifest revisions (added / removed / metadata) =="
curl -s -X POST "http://127.0.0.1:$PORT_FIXTURE/__control" -H 'Content-Type: application/json' \
  -d '{"action":"set_revision","path":"collections/root.json","revision":"revisions/root-r1.json"}' >/dev/null
curl -s -X POST "http://127.0.0.1:$PORT_FIXTURE/__control" -H 'Content-Type: application/json' \
  -d '{"action":"set_revision","path":"manifests/asia.json","revision":"revisions/asia-r1.json"}' >/dev/null
curl -s -X POST "http://127.0.0.1:$PORT_FIXTURE/__control" -H 'Content-Type: application/json' \
  -d '{"action":"set_revision","path":"manifests/legacy-v2.json","revision":"revisions/legacy-r1.json"}' >/dev/null
echo "    three documents switched to revised variants"

echo "== 7. revision check over the whole reachable graph =="
python3 -m dlcatalog harvest "$SEED" --db "$DB" --mode refetch \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print('   run', r['id'], r['status'], 'changed records:', r['changed'])"

echo "== 8. exporting the catalog with provenance source paths =="
EXPORT_PATH=$(python3 -m dlcatalog export --db "$DB" --out "$EXPORT_DIR")
echo "    $EXPORT_PATH"
python3 - "$EXPORT_PATH" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print("   summary:", payload["summary"])
shared = next(n for n in payload["nodes"] if n["url"].endswith("shared.json"))
print("   shared manifest membership paths:", len(shared["source_paths"]))
for path in shared["source_paths"]:
    print("     -", " -> ".join(p.split("/")[-1] for p in path))
PY

echo
echo "== done. web UI still available at http://127.0.0.1:$PORT_APP =="
echo "   press Ctrl+C to stop both servers"
wait
