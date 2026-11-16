"""The sample feed and the README excerpt are generated from the fixtures; they must not drift."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import example  # noqa: E402
from helpers import SCHEMA, fc  # noqa: E402


class ExampleTests(unittest.TestCase):
    def test_the_sample_feed_is_current_and_valid(self):
        snap = example.example_snapshot()
        self.assertEqual(fc.validate(snap, SCHEMA), [])
        self.assertEqual(example.SAMPLE.read_text(), example.sample_text(snap), "run: python3 tests/example.py --write")
        self.assertEqual(json.loads(example.SAMPLE.read_text()), snap)

    def test_the_sample_covers_every_source_and_names_no_real_account(self):
        snap = json.loads(example.SAMPLE.read_text())
        self.assertEqual([s["id"] for s in snap["sources"]], [sid for sid in fc.SOURCE_IDS if sid not in ("anthropic_ledger",)])
        self.assertTrue(all(s["ok"] for s in snap["sources"]))
        text = example.SAMPLE.read_text()
        self.assertFalse(fc.has_secret(text))

    def test_the_readme_excerpt_is_current(self):
        text = example.README.read_text()
        self.assertEqual(text.count(example.START), 1)
        block = text[text.index(example.START):text.index(example.END) + len(example.END)]
        self.assertEqual(block, example.readme_block(), "run: python3 tests/example.py --write")


if __name__ == "__main__":
    unittest.main()
