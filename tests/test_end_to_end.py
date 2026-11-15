"""End-to-end tests against the real fixture HTTP server.

Covers the whole pipeline over real sockets: fixture generation, crawling
with cycles and transient failures, control-API revision switching, a
simulated process restart (new Store + new Harvester on the same database),
single-node refetch and the provenance-aware export.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request

from dlcatalog.export import build_export
from dlcatalog.fixture.generate import generate
from dlcatalog.fixture.server import make_server
from dlcatalog.harvester import Harvester
from dlcatalog.models import CONFIRMED, FAILED
from dlcatalog.store import Store


def _control(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read())


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = 8137
        cls.manifest = generate(cls.tmp.name, port=cls.port)
        cls.httpd = make_server(cls.tmp.name, "127.0.0.1", cls.port)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.seed = cls.manifest["seed_url"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        # Reset control state before every test.
        urllib.request.urlopen(self.base + "/__control/reset", timeout=10).read()
        self.work = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.work.name, "catalog.db")

    def tearDown(self):
        self.work.cleanup()

    def test_full_crawl_cycles_failures_and_provenance(self):
        # Flaky manifest fails its first request only; after a restart the
        # resumed crawl must find it healthy and confirm it.
        _control(self.base + "/__control", {
            "action": "set_flaky",
            "path": "manifests/flaky.json", "fail_times": 1,
        })

        store = Store(self.db)
        harvester = Harvester(store)
        run_id = harvester.run(self.seed)
        run = store.get_run(run_id)
        self.assertEqual(run["status"], "done")
        self.assertGreaterEqual(run["failed"], 1)

        states = {r["url"]: r["state"] for r in store.list_nodes()}
        # Cycle pair exists with both edges, each fetched once.
        special = self.base + "/collections/special.json"
        loopback = self.base + "/collections/loopback.json"
        edges = set(store.all_edges())
        self.assertIn((special, loopback), edges)
        self.assertIn((loopback, special), edges)

        # Permanent 404 stays failed; flaky is also failed this run.
        missing = self.base + "/manifests/missing.json"
        self.assertEqual(states[missing], FAILED)
        flaky = self.base + "/manifests/flaky.json"
        self.assertEqual(states[flaky], FAILED)

        # Shared manifest is referenced from four documents (maps, special,
        # shared-col and root) — every membership must survive the crawl.
        shared = self.base + "/manifests/shared.json"
        parents = sorted(r["url"] for r in store.get_parents(shared))
        self.assertEqual(len(parents), 4)

        # Every provenance route to shared is captured and cycle-free.
        paths = store.source_paths(self.seed)
        shared_paths = paths[shared]
        self.assertGreaterEqual(len(shared_paths), 3)
        for path in shared_paths:
            self.assertEqual(len(path), len(set(path)))
            self.assertEqual(path[0], self.seed)
        store.close()

        # --- Simulated process restart: same DB, fresh objects. ---
        store2 = Store(self.db)
        Harvester(store2).run(self.seed)
        states2 = {r["url"]: r["state"] for r in store2.list_nodes()}
        self.assertEqual(states2[flaky], CONFIRMED)  # flaky now succeeds
        self.assertEqual(states2[missing], FAILED)   # permanent failure remains
        store2.close()

    def test_revisions_mark_added_removed_and_metadata_changes(self):
        store = Store(self.db)
        harvester = Harvester(store)
        harvester.run(self.seed)

        # Switch three documents to their revised variants.
        _control(self.base + "/__control", {
            "action": "set_revision",
            "path": "collections/root.json",
            "revision": "revisions/root-r1.json"})
        _control(self.base + "/__control", {
            "action": "set_revision",
            "path": "manifests/asia.json",
            "revision": "revisions/asia-r1.json"})
        _control(self.base + "/__control", {
            "action": "set_revision",
            "path": "manifests/legacy-v2.json",
            "revision": "revisions/legacy-r1.json"})

        # Single-node refetch of root first.
        root_run = harvester.refetch_node(self.seed)
        root_changes = store.list_changes(run_id=root_run)
        kinds = {(c["change_kind"], c["resource_url"]) for c in root_changes}
        self.assertTrue(any(k == "added" and "new-shelf" in url
                            for k, url in kinds))
        self.assertTrue(any(k == "removed" and "special" in url
                            for k, url in kinds))

        # Full revision check picks up manifest metadata and image changes.
        full_run = harvester.run(self.seed, mode="refetch")
        changes = store.list_changes(run_id=full_run)
        asia = self.base + "/manifests/asia.json"
        asia_changes = [c for c in changes if c["node_url"] == asia]
        self.assertTrue(any(c["change_kind"] == "metadata_changed"
                            and c["resource_url"].endswith("asia-1.jpg")
                            for c in asia_changes))
        self.assertTrue(any(c["change_kind"] == "added"
                            and c["resource_url"].endswith("asia-3.jpg")
                            for c in asia_changes))
        legacy = self.base + "/manifests/legacy-v2.json"
        self.assertTrue(any(
            c["node_url"] == legacy and c["detail"] == "titles changed"
            for c in changes))

        # A second refetch with unchanged documents yields no new changes.
        idle_run = harvester.run(self.seed, mode="refetch")
        self.assertEqual(store.list_changes(run_id=idle_run), [])
        store.close()

    def test_export_contains_source_paths_and_only_confirmed(self):
        _control(self.base + "/__control", {
            "action": "set_flaky",
            "path": "manifests/flaky.json", "fail_times": 5,
        })
        store = Store(self.db)
        Harvester(store).run(self.seed)

        payload = build_export(store, self.seed)
        self.assertIn("exported_at", payload)
        self.assertEqual(payload["seed_url"], self.seed)
        # Failed nodes never appear as catalog entries.
        exported_urls = {n["url"] for n in payload["nodes"]}
        missing = self.base + "/manifests/missing.json"
        self.assertNotIn(missing, exported_urls)
        self.assertTrue(any(n["url"] == missing
                            for n in payload["failed_nodes"]))

        shared = self.base + "/manifests/shared.json"
        shared_record = next(n for n in payload["nodes"]
                             if n["url"] == shared)
        self.assertGreaterEqual(len(shared_record["source_paths"]), 3)
        # Multilingual titles retain language tags.
        langs = {t["language"] for t in shared_record["titles"]}
        self.assertIn("en", langs)
        self.assertIn("de", langs)
        # Images carry address + dimensions only.
        asia = next(n for n in payload["nodes"]
                    if n["url"].endswith("asia.json"))
        self.assertTrue(asia["images"])
        for image in asia["images"]:
            self.assertEqual(set(image.keys()), {"url", "width", "height"})
        # Fallback title provenance is exported.
        nolabel = next(n for n in payload["nodes"]
                       if n["url"].endswith("nolabel.json"))
        self.assertTrue(nolabel["title_source"].startswith("fallback:"))
        store.close()

    def test_control_state_survives_server_restart(self):
        state_file = os.path.join(self.tmp.name, "server-state.json")
        _control(self.base + "/__control", {
            "action": "set_flaky",
            "path": "manifests/flaky.json", "fail_times": 1,
        })
        self.assertTrue(os.path.exists(state_file))

        # Restart the HTTP server against the same directory.
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = make_server(self.tmp.name, "127.0.0.1", self.port)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()

        with urllib.request.urlopen(
                self.base + "/__control/state", timeout=10) as res:
            state = json.loads(res.read())
        self.assertEqual(
            state["flaky_remaining"]["manifests/flaky.json"], 1)

        # The pending failure actually fires after restart.
        try:
            urllib.request.urlopen(
                self.base + "/manifests/flaky.json", timeout=10)
            self.fail("expected 503 from persisted flaky state")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 503)
        # And the next request succeeds.
        with urllib.request.urlopen(
                self.base + "/manifests/flaky.json", timeout=10) as res:
            self.assertEqual(res.status, 200)


if __name__ == "__main__":
    unittest.main()
