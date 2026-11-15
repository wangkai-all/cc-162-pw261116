"""Harvester tests driven by an in-memory fake fetcher (no sockets)."""
import os
import tempfile
import unittest

from dlcatalog.harvester import Harvester, normalize_url
from dlcatalog.httpclient import FetchResult
from dlcatalog.models import CONFIRMED, FAILED, PENDING
from dlcatalog.store import Store


def collection(url, members, label=None):
    items = [{"id": m["url"], "type": "Collection" if m["type"] == "collection"
              else "Manifest",
              "label": {"en": [m.get("label", m["url"])]}} for m in members]
    doc = {"@context": "x", "id": url, "type": "Collection", "items": items}
    if label:
        doc["label"] = {"en": [label]}
    return doc


def manifest(url, image_urls):
    return {
        "@context": "x", "id": url, "type": "Manifest",
        "label": {"en": [f"Manifest {url}"]},
        "items": [{
            "id": f"{url}/canvas/{i + 1}", "type": "Canvas",
            "items": [{"type": "AnnotationPage", "items": [{
                "type": "Annotation",
                "body": {"id": img, "type": "Image",
                         "width": 100, "height": 200},
            }]}],
        } for i, img in enumerate(image_urls)],
    }


class FakeFetcher:
    """Serves documents from a dict; supports failure scripts and revisions."""

    def __init__(self, docs):
        self.docs = docs
        self.calls = []
        # url -> number of consecutive failures to return first
        self.fail_times = {}
        # url -> replacement document
        self.revisions = {}

    def __call__(self, url, timeout=20.0, etag=None, last_modified=None):
        import json
        url = normalize_url(url)
        self.calls.append(url)
        remaining = self.fail_times.get(url, 0)
        if remaining > 0:
            self.fail_times[url] = remaining - 1
            return FetchResult(url=url, ok=False, status=503,
                               error="HTTP 503: simulated")
        doc = self.revisions.get(url, self.docs.get(url))
        if doc is None:
            return FetchResult(url=url, ok=False, status=404,
                               error="HTTP 404: not found")
        body = json.dumps(doc).encode("utf-8")
        return FetchResult(url=url, ok=True, status=200, body=body,
                           content_type="application/json",
                           etag='"v1"', last_modified=None)


class HarvesterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class CycleTests(HarvesterTestBase):
    def test_mutual_references_fetch_each_url_once(self):
        docs = {
            "root": collection("root", [{"url": "a", "type": "collection"}]),
            "a": collection("a", [
                {"url": "b", "type": "collection"},
                {"url": "m1", "type": "manifest"}], label="A"),
            "b": collection("b", [{"url": "a", "type": "collection"}],
                            label="B"),
            "m1": manifest("m1", ["img1"]),
        }
        fetcher = FakeFetcher(docs)
        harvester = Harvester(self.store, fetcher=fetcher)
        harvester.run("root")

        # Every URL fetched exactly once despite the a <-> b cycle.
        self.assertEqual(sorted(fetcher.calls), ["a", "b", "m1", "root"])
        self.assertEqual(set(self.store.count_by_state()), {"confirmed"})
        # Membership edges still record both directions.
        edges = set(self.store.all_edges())
        self.assertIn(("a", "b"), edges)
        self.assertIn(("b", "a"), edges)
        # Provenance paths: cycle-free by construction, yet b is reachable
        # and the back-edge is recorded as an edge.
        paths = self.store.source_paths("root")
        self.assertIn(["root", "a"], paths["a"])
        self.assertIn(["root", "a", "b"], paths["b"])
        for node_paths in paths.values():
            for path in node_paths:
                self.assertEqual(len(path), len(set(path)), path)

    def test_shared_manifest_keeps_every_collection_path(self):
        docs = {
            "root": collection("root", [
                {"url": "c1", "type": "collection"},
                {"url": "c2", "type": "collection"}]),
            "c1": collection("c1", [{"url": "shared", "type": "manifest"}]),
            "c2": collection("c2", [{"url": "shared", "type": "manifest"}]),
            "shared": manifest("shared", ["img"]),
        }
        fetcher = FakeFetcher(docs)
        Harvester(self.store, fetcher=fetcher).run("root")
        # Document downloaded once ...
        self.assertEqual(fetcher.calls.count("shared"), 1)
        # ... but both memberships preserved.
        parents = sorted(r["url"] for r in self.store.get_parents("shared"))
        self.assertEqual(parents, ["c1", "c2"])
        paths = self.store.source_paths("root")["shared"]
        self.assertIn(["root", "c1", "shared"], paths)
        self.assertIn(["root", "c2", "shared"], paths)


class ResumeTests(HarvesterTestBase):
    def _docs(self):
        return {
            "root": collection("root", [
                {"url": "flaky", "type": "manifest"},
                {"url": "ok", "type": "manifest"}]),
            "flaky": manifest("flaky", ["img-f"]),
            "ok": manifest("ok", ["img-o"]),
        }

    def test_failed_node_resumed_on_next_run(self):
        docs = self._docs()
        fetcher = FakeFetcher(docs)
        fetcher.fail_times["flaky"] = 1
        harvester = Harvester(self.store, fetcher=fetcher)
        run1 = harvester.run("root")

        failed = self.store.list_failed()
        self.assertEqual([r["url"] for r in failed], ["flaky"])
        run_row = self.store.get_run(run1)
        self.assertEqual(run_row["failed"], 1)
        # Pending/failure state survives reopening the database (restart).
        self.store.close()
        self.store = Store(self.db)

        harvester2 = Harvester(self.store, fetcher=fetcher)
        harvester2.run("root")
        states = {r["url"]: r["state"] for r in self.store.list_nodes()}
        self.assertEqual(states["flaky"], CONFIRMED)
        self.assertEqual(states["ok"], CONFIRMED)
        self.assertEqual(self.store.list_failed(), [])

    def test_permanent_404_stays_failed_and_others_confirm(self):
        docs = self._docs()
        del docs["flaky"]
        fetcher = FakeFetcher(docs)
        Harvester(self.store, fetcher=fetcher).run("root")
        row = self.store.get_node("flaky")
        self.assertEqual(row["state"], FAILED)
        self.assertEqual(row["http_status"], 404)
        self.assertEqual(self.store.get_node("ok")["state"], CONFIRMED)


class RefetchTests(HarvesterTestBase):
    def test_refetch_all_detects_added_removed_and_metadata(self):
        docs = {
            "root": collection("root", [
                {"url": "m1", "type": "manifest"},
                {"url": "m2", "type": "manifest"}]),
            "m1": manifest("m1", ["img1"]),
            "m2": manifest("m2", ["img2"]),
        }
        fetcher = FakeFetcher(docs)
        harvester = Harvester(self.store, fetcher=fetcher)
        harvester.run("root")

        # Revision: m2 leaves root, m3 arrives; m1 image changes dimensions.
        docs["root"] = collection("root", [
            {"url": "m1", "type": "manifest"},
            {"url": "m3", "type": "manifest"}])
        docs["m3"] = manifest("m3", ["img3"])
        revised_m1 = manifest("m1", ["img1"])
        revised_m1["items"][0]["items"][0]["items"][0]["body"]["width"] = 999
        docs["m1"] = revised_m1

        run2 = harvester.run("root", mode="refetch")
        changes = self.store.list_changes(run_id=run2)
        kinds = {(c["change_kind"], c["resource_url"]) for c in changes}
        self.assertIn(("added", "m3"), kinds)
        self.assertIn(("removed", "m2"), kinds)
        self.assertIn(("metadata_changed", "img1"), kinds)
        # m2 node remains catalogued; only its edge from root disappeared.
        self.assertIsNotNone(self.store.get_node("m2"))
        self.assertEqual(self.store.get_children("root")[0]["url"], "m1")

    def test_single_node_refetch_and_crawl_new_children(self):
        docs = {
            "root": collection("root", [{"url": "c", "type": "collection"}]),
            "c": collection("c", [{"url": "m1", "type": "manifest"}]),
            "m1": manifest("m1", ["img1"]),
        }
        fetcher = FakeFetcher(docs)
        harvester = Harvester(self.store, fetcher=fetcher)
        harvester.run("root")

        # A newly added member under c must be discovered by node refetch.
        docs["c"] = collection("c", [
            {"url": "m1", "type": "manifest"},
            {"url": "m2", "type": "manifest"}])
        docs["m2"] = manifest("m2", ["img2"])
        run_id = harvester.refetch_node("c")
        self.assertEqual(self.store.get_node("m2")["state"], CONFIRMED)
        changes = self.store.list_changes(run_id=run_id)
        self.assertTrue(any(c["change_kind"] == "added"
                            and c["resource_url"] == "m2" for c in changes))

    def test_single_node_refetch_retries_failed_node(self):
        docs = {
            "root": collection("root", [{"url": "flaky", "type": "manifest"}]),
            "flaky": manifest("flaky", ["img"]),
        }
        fetcher = FakeFetcher(docs)
        fetcher.fail_times["flaky"] = 3
        harvester = Harvester(self.store, fetcher=fetcher)
        harvester.run("root")
        self.assertEqual(self.store.get_node("flaky")["state"], FAILED)

        fetcher.fail_times["flaky"] = 0
        harvester.refetch_node("flaky")
        self.assertEqual(self.store.get_node("flaky")["state"], CONFIRMED)


class UrlTests(unittest.TestCase):
    def test_fragment_removed_query_kept(self):
        self.assertEqual(
            normalize_url("https://x.test/a?x=1#frag"),
            "https://x.test/a?x=1")


if __name__ == "__main__":
    unittest.main()
