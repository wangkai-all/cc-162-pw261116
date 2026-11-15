"""Tests for revision diffing and the SQLite state store."""
import json
import os
import tempfile
import unittest

from dlcatalog.diff import ADDED, METADATA_CHANGED, REMOVED, diff_nodes
from dlcatalog.iiif import parse_document
from dlcatalog.models import FAILED, PENDING, Image, Title
from dlcatalog.store import Store


def v3_collection(url, member_urls):
    return {
        "@context": "x", "id": url, "type": "Collection",
        "label": {"en": ["C"]},
        "items": [{"id": m, "type": "Manifest",
                   "label": {"en": [f"M {m}"]}} for m in member_urls],
    }


def v3_manifest(url, images, label="M"):
    return {
        "@context": "x", "id": url, "type": "Manifest",
        "label": {"en": [label]},
        "items": [{
            "id": f"{url}/canvas/{i + 1}", "type": "Canvas",
            "items": [{"type": "AnnotationPage", "items": [{
                "type": "Annotation",
                "body": {"id": img["url"], "type": "Image",
                         "width": img.get("width"),
                         "height": img.get("height")},
            }]}],
        } for i, img in enumerate(images)],
    }


class DiffTests(unittest.TestCase):
    def test_member_added_and_removed(self):
        url = "https://x.test/c"
        old = parse_document(
            v3_collection(url, ["https://x.test/m1", "https://x.test/m2"]),
            url)
        new = parse_document(
            v3_collection(url, ["https://x.test/m2", "https://x.test/m3"]),
            url)
        kinds = {(c.change_kind, c.resource_url) for c in diff_nodes(old, new)}
        self.assertIn((ADDED, "https://x.test/m3"), kinds)
        self.assertIn((REMOVED, "https://x.test/m1"), kinds)
        self.assertNotIn((ADDED, "https://x.test/m2"), kinds)

    def test_image_dimensions_metadata_change(self):
        url = "https://x.test/m"
        old = parse_document(v3_manifest(
            url, [{"url": "i1", "width": 10, "height": 10}]), url)
        new = parse_document(v3_manifest(
            url, [{"url": "i1", "width": 12, "height": 10}]), url)
        changes = diff_nodes(old, new)
        meta = [c for c in changes if c.change_kind == METADATA_CHANGED]
        self.assertTrue(any(c.resource_url == "i1" for c in meta))

    def test_image_added_and_removed(self):
        url = "https://x.test/m"
        old = parse_document(
            v3_manifest(url, [{"url": "i1"}, {"url": "i2"}]), url)
        new = parse_document(
            v3_manifest(url, [{"url": "i2"}, {"url": "i3"}]), url)
        kinds = {(c.change_kind, c.resource_url) for c in diff_nodes(old, new)}
        self.assertIn((ADDED, "i3"), kinds)
        self.assertIn((REMOVED, "i1"), kinds)

    def test_title_language_change_is_metadata(self):
        url = "https://x.test/m"
        old = parse_document(v3_manifest(url, [], label="English"), url)
        new_doc = v3_manifest(url, [], label="English")
        new_doc["label"] = {"en": ["English"], "zh": ["英语"]}
        new = parse_document(new_doc, url)
        changes = diff_nodes(old, new)
        self.assertTrue(any(c.detail == "titles changed" for c in changes))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _parsed(self, url, kind="collection", members=()):
        doc = v3_collection(url, list(members)) if kind == "collection" \
            else v3_manifest(url, [{"url": f"{url}/img", "width": 5}])
        return parse_document(doc, url)

    def test_discovered_pending_then_confirmed(self):
        self.assertTrue(self.store.upsert_discovered("u1", "manifest"))
        self.assertFalse(self.store.upsert_discovered("u1", "manifest"))
        self.assertEqual([r["url"] for r in self.store.list_nodes(
            state=PENDING)], ["u1"])
        self.store.save_fetched(self._parsed("u1", "manifest"), 200, None, None)
        counts = self.store.count_by_state()
        self.assertEqual(counts["confirmed"], 1)
        self.assertNotIn("pending", counts)

    def test_failure_records_error_and_status(self):
        self.store.upsert_discovered("u-bad", None)
        self.store.save_failed("u-bad", 503, "simulated transient failure")
        row = self.store.get_node("u-bad")
        self.assertEqual(row["state"], FAILED)
        self.assertEqual(row["http_status"], 503)
        self.assertIn("transient", row["error"])

    def test_edges_preserve_multiple_parents(self):
        run = self.store.create_run("seed", "crawl")
        for parent in ("p1", "p2"):
            self.store.upsert_discovered(parent, "collection")
            self.store.upsert_discovered("shared", "manifest")
            self.store.record_edge(parent, "shared", run)
        parents = [r["url"] for r in self.store.get_parents("shared")]
        self.assertEqual(sorted(parents), ["p1", "p2"])

    def test_replace_edges_reports_added_removed_keeps_node(self):
        run = self.store.create_run("seed", "crawl")
        self.store.upsert_discovered("p", "collection")
        for child in ("m1", "m2"):
            self.store.upsert_discovered(child, "manifest")
            self.store.record_edge("p", child, run)
        added, removed = self.store.replace_edges("p", ["m2", "m3"], run)
        self.assertEqual(added, ["m3"])
        self.assertEqual(removed, ["m1"])
        # m1 stays in the catalog because edges are memberships, not ownership.
        self.assertIsNotNone(self.store.get_node("m1"))

    def test_source_paths_cover_diamond_and_terminate_on_cycle(self):
        run = self.store.create_run("seed", "crawl")
        edges = [("root", "a"), ("root", "b"), ("a", "shared"),
                 ("b", "shared"), ("shared", "loop"), ("loop", "shared")]
        for parent, child in edges:
            self.store.upsert_discovered(parent, "collection")
            self.store.upsert_discovered(child, "collection")
            self.store.record_edge(parent, child, run)
        paths = self.store.source_paths("root")
        self.assertIn(["root", "a", "shared"], paths["shared"])
        self.assertIn(["root", "b", "shared"], paths["shared"])
        # The loop -> shared back-edge must not produce infinite paths.
        self.assertTrue(all(len(p) <= 6 for p in paths["shared"]))
        for route in paths["shared"]:
            self.assertEqual(len(route), len(set(route)))

    def test_changes_deduped_within_run(self):
        run = self.store.create_run("seed", "crawl")
        from dlcatalog.diff import Change
        change = Change("n", ADDED, "r", "manifest", "member added")
        first = self.store.record_changes(run, [change])
        second = self.store.record_changes(run, [change])
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_images_re_extracted_from_stored_raw(self):
        parsed = self._parsed("m1", "manifest")
        self.store.save_fetched(parsed, 200, None, None)
        images = self.store.images_for("m1")
        self.assertEqual(images, [Image(url="m1/img", width=5, height=None)])

    def test_persistence_across_reopen(self):
        self.store.upsert_discovered("u1", "manifest")
        run = self.store.create_run("seed", "crawl")
        self.store.record_edge("seed", "u1", run)
        self.store.close()
        reopened = Store(self.db)
        self.assertEqual(reopened.list_pending(), ["u1"])
        self.assertEqual(reopened.all_edges(), [("seed", "u1")])
        reopened.close()


if __name__ == "__main__":
    unittest.main()
