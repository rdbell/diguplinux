import tempfile
import unittest
from pathlib import Path

from diguplinux.crawl import scan
from diguplinux.ignore import matches, validate_patterns
from diguplinux.runtime import DigupError


class IgnoreTests(unittest.TestCase):
    def test_patterns_are_relative_to_each_root_in_code_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            roots = [Path(folder) / name for name in ("one", "two")]
            for root in roots:
                (root / "drafts").mkdir(parents=True)
                (root / "drafts/private.py").write_text("private")
                (root / "keep.py").write_text("keep")
                (root / "skip.py").write_text("skip")
            result = scan(roots, code=True, ignore_patterns=["drafts/", "skip.*"])
            self.assertEqual(set(result.files), {root / "keep.py" for root in roots})
            self.assertEqual(result.skipped, 2)

    def test_glob_semantics(self):
        for path, pattern, directory, expected in [
            ("nested/file.tmp", "*.tmp", False, True),
            ("nested/file.TMP", "*.tmp", False, False),
            ("drafts", "**/drafts/**", True, True),
            ("nested/drafts/file.txt", "**/drafts/**", False, True),
            ("reports/a.txt", "reports/*.txt", False, True),
            ("reports/nested/a.txt", "reports/*.txt", False, False),
            ("reports/nested/a.txt", "reports/**/*.txt", False, True),
            ("nested/reports/a.txt", "reports/**/*.txt", False, False),
            ("nested/drafts", "drafts/", True, True),
            ("drafts", "drafts/", False, False),
            ("notes1.txt", "notes[12].?xt", False, True),
        ]:
            with self.subTest(path=path, pattern=pattern):
                self.assertEqual(matches(Path(path), pattern, directory), expected)

    def test_invalid_patterns(self):
        for patterns in ("*.tmp", [1], [""], ["/tmp/*"], ["!keep.txt"], ["../private"], ["\0"]):
            with self.subTest(patterns=patterns), self.assertRaises(DigupError):
                validate_patterns(patterns)
