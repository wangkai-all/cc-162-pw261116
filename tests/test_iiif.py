"""Tests for the tolerant IIIF v2/v3 parser."""
import unittest

from dlcatalog.iiif import parse_document, parse_titles
from dlcatalog.models import COLLECTION, MANIFEST, UNKNOWN


class TitleTests(unittest.TestCase):
    def test_v3_language_map_keeps_tags(self):
        titles = parse_titles({"en": ["Hello"], "zh": ["你好"],
                               "@none": ["Fallback"]})
        self.assertEqual(len(titles), 3)
        by_lang = {t.language: t.text for t in titles}
        self.assertEqual(by_lang["en"], "Hello")
        self.assertEqual(by_lang["zh"], "你好")
        self.assertEqual(by_lang[None], "Fallback")

    def test_v2_string_and_value_objects(self):
        titles = parse_titles([
            {"@value": "Old title", "@language": "en"},
            "Plain string",
        ])
        self.assertEqual(titles[0].text, "Old title")
        self.assertEqual(titles[0].language, "en")
        self.assertEqual(titles[1].text, "Plain string")
        self.assertIsNone(titles[1].language)

    def test_blank_strings_ignored(self):
        self.assertEqual(parse_titles("   "), [])
        self.assertEqual(parse_titles(None), [])


class FallbackTests(unittest.TestCase):
    def test_falls_back_to_summary_then_id(self):
        doc = {"@context": "x", "id": "https://example.test/m1",
               "type": "Manifest",
               "summary": [{"@value": "A summary"}], "items": []}
        parsed = parse_document(doc, doc["id"])
        self.assertEqual(parsed.titles[0].text, "A summary")
        self.assertEqual(parsed.title_source, "fallback:summary")

        doc2 = {"id": "https://example.test/m2", "type": "Manifest",
                "items": []}
        parsed2 = parse_document(doc2, doc2["id"])
        self.assertEqual(parsed2.titles[0].text, "https://example.test/m2")
        self.assertEqual(parsed2.title_source, "fallback:id")


class CollectionTests(unittest.TestCase):
    def test_v3_members_with_relative_urls(self):
        doc = {
            "@context": "x", "id": "https://example.test/c/root",
            "type": "Collection", "label": {"en": ["Root"]},
            "items": [
                {"id": "child.json", "type": "Collection",
                 "label": {"en": ["Child"]}},
                {"id": "https://other.test/m", "type": "Manifest"},
            ],
        }
        parsed = parse_document(doc, doc["id"])
        self.assertEqual(parsed.node_type, COLLECTION)
        self.assertEqual(
            parsed.members[0].url, "https://example.test/c/child.json")
        self.assertEqual(parsed.members[0].type_hint, COLLECTION)
        self.assertEqual(parsed.members[1].type_hint, MANIFEST)
        self.assertEqual(parsed.members[1].label, [])

    def test_v2_members_merged_and_deduped(self):
        doc = {
            "@context": "x", "@id": "https://example.test/c",
            "@type": "sc:Collection", "label": "Old",
            "collections": [{"@id": "https://example.test/sub",
                             "@type": "sc:Collection"}],
            "manifests": [{"@id": "https://example.test/m1",
                           "@type": "sc:Manifest", "label": "M1"}],
            "members": [{"@id": "https://example.test/m1",
                         "@type": "sc:Manifest"}],
        }
        parsed = parse_document(doc, doc["@id"])
        urls = [m.url for m in parsed.members]
        self.assertEqual(urls.count("https://example.test/m1"), 1)
        self.assertIn("https://example.test/sub", urls)


class ManifestImageTests(unittest.TestCase):
    def test_v3_images_address_and_dimensions_only(self):
        doc = {
            "@context": "x", "id": "https://example.test/m",
            "type": "Manifest", "label": "M",
            "items": [{
                "id": "canvas1", "type": "Canvas",
                "items": [{
                    "type": "AnnotationPage",
                    "items": [{
                        "type": "Annotation",
                        "body": {"id": "https://img.test/a.jpg",
                                 "type": "Image", "width": 100,
                                 "height": 200},
                    }],
                }],
            }],
        }
        parsed = parse_document(doc, doc["id"])
        self.assertEqual(parsed.node_type, MANIFEST)
        self.assertEqual(len(parsed.images), 1)
        img = parsed.images[0]
        self.assertEqual(img.url, "https://img.test/a.jpg")
        self.assertEqual((img.width, img.height), (100, 200))

    def test_v2_sequences_images(self):
        doc = {
            "@context": "x", "@id": "https://example.test/m2",
            "@type": "sc:Manifest", "label": "M2",
            "sequences": [{
                "canvases": [{
                    "images": [{
                        "resource": {"@id": "https://img.test/b.jpg",
                                     "width": 50, "height": 60},
                    }],
                }],
            }],
        }
        parsed = parse_document(doc, doc["@id"])
        self.assertEqual(parsed.node_type, MANIFEST)
        self.assertEqual(parsed.images[0].url, "https://img.test/b.jpg")
        self.assertEqual(parsed.images[0].width, 50)

    def test_unknown_document(self):
        parsed = parse_document({"hello": "world"}, "https://x.test/z")
        self.assertEqual(parsed.node_type, UNKNOWN)


if __name__ == "__main__":
    unittest.main()
