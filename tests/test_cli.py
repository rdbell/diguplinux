import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diguplinux.cli import main
from diguplinux.config import PIPELINE_VERSION
from diguplinux.extract import chunks
from diguplinux.indexer import index
from diguplinux.runtime import DigupError, lock
from diguplinux.search import search
from diguplinux.store import Store


class FakeWorker:
    """Independent tiny embeddings to exercise ranking and persistence without model downloads."""

    def __init__(self, media=False):
        pass

    def embed(self, kind, value):
        value = value.lower()
        if any(t in value for t in ("pet", "animal", "tenant")):
            return [1.0, 0.0, 0.0]
        if any(t in value for t in ("cook", "tomato", "sauce")):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def close(self):
        pass


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "config")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = self.base / "documents"
        self.root.mkdir()
        self.folder = self.base / "index"
        self.store = Store(self.folder)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def index(self, **options):
        with contextlib.redirect_stderr(io.StringIO()):
            return index(self.store, [self.root], **options)

    def test_incremental_changes_deletes_and_root_scoping(self):
        first = self.write("lease.md", "Animals are allowed in this apartment.")
        other = self.base / "other"
        other.mkdir()
        (other / "unrelated.txt").write_text("Unrelated folder")
        with contextlib.redirect_stderr(io.StringIO()):
            index(self.store, [other], keyword_only=True)
        self.assertEqual(self.index(keyword_only=True)["indexed"], 1)
        self.assertEqual(self.index(keyword_only=True)["unchanged"], 1)
        first.write_text("Updated tenant agreement: no pets allowed.")
        self.assertEqual(self.index(keyword_only=True)["indexed"], 1)
        self.assertEqual(len(search(self.store, "tenant", keyword_only=True)), 1)
        self.assertEqual(search(self.store, "Animals", keyword_only=True), [])
        first.unlink()
        self.assertEqual(self.index(keyword_only=True)["removed"], 1)
        self.assertEqual(self.store.status()["files"], 1)
        self.assertEqual(search(self.store, "tenant", keyword_only=True), [])

    def test_selection_changes_reuse_legacy_cached_vectors(self):
        keep = self.write("keep.txt", "Keep these embeddings")
        self.write("draft.txt", "Draft embeddings")
        self.index(worker_factory=FakeWorker)
        # Simulate a partially completed pass: some rows have patterns, others do not.
        for row in self.store.db.execute("SELECT id,pipeline FROM files").fetchall():
            pipeline = json.loads(row["pipeline"])
            pipeline["options"].update(excludes=["/old/exclusion"], exclude_ext=[".csv"])
            if row["id"] % 2:
                pipeline["options"]["ignore_patterns"] = ["old-pattern.*"]
            with self.store.db:
                self.store.db.execute(
                    "UPDATE files SET pipeline=? WHERE id=?", (json.dumps(pipeline), row["id"])
                )
        before = self.store.db.execute(
            "SELECT s.id,s.vector FROM segments s JOIN files f ON f.id=s.file_id WHERE f.path=?",
            (str(keep),),
        ).fetchall()
        with patch("diguplinux.indexer.extract", side_effect=AssertionError("must reuse cache")):
            result = self.index(ignore_patterns=["draft.*"], worker_factory=FakeWorker)
        self.assertEqual((result["indexed"], result["unchanged"], result["removed"]), (0, 1, 1))
        after = self.store.db.execute("SELECT id,vector FROM segments").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        result = self.index(worker_factory=FakeWorker)
        self.assertEqual((result["indexed"], result["unchanged"]), (1, 1))
        for selection in ({"excludes": [self.root / "absent"]}, {"exclude_ext": {".csv"}}):
            with patch("diguplinux.indexer.extract", side_effect=AssertionError("must reuse cache")):
                self.assertEqual(self.index(**selection)["unchanged"], 2)

    def test_extraction_changes_and_reindex_still_invalidate_cache(self):
        self.write("notes.txt", "Notes")
        self.index(worker_factory=FakeWorker)
        for options in (
            {"language": "spa"},
            {"max_segments": 6000},
            {"video_interval": 10},
            {"keyword_only": True},
            {"reindex": True},
        ):
            with self.subTest(options=options):
                self.assertEqual(self.index(worker_factory=FakeWorker, **options)["indexed"], 1)
                self.index(worker_factory=FakeWorker)
        with self.store.db:
            self.store.db.execute("UPDATE files SET pipeline=?", ('{"version":"old","options":{}}',))
        self.assertEqual(self.index(worker_factory=FakeWorker)["indexed"], 1)
        with self.store.db:
            self.store.db.execute("UPDATE files SET pipeline='invalid json'")
        self.assertEqual(self.index(worker_factory=FakeWorker)["indexed"], 1)

    def test_semantics_without_shared_words_and_keyword_numbers(self):
        lease = self.write("lease.txt", "Tenant may keep domestic animals. Agreement INV-98127.")
        self.write("recipe.txt", "Cook the tomato sauce.")
        self.index(worker_factory=FakeWorker)
        result = search(self.store, "pets", worker_factory=FakeWorker)
        self.assertEqual(result[0]["path"], str(lease))
        self.assertFalse(result[0]["keyword_match"])
        result = search(self.store, "INV-98127", keyword_only=True)
        self.assertEqual(result[0]["path"], str(lease))
        self.assertTrue(result[0]["keyword_match"])

    def test_upgrade_keyword_index_to_semantic(self):
        self.write("lease.md", "Animals")
        self.index(keyword_only=True)
        with self.assertRaisesRegex(DigupError, "No semantic vectors"):
            search(self.store, "pets", worker_factory=FakeWorker)
        self.assertEqual(self.index(worker_factory=FakeWorker)["indexed"], 1)
        self.assertEqual(self.store.status()["vectors"], 1)

    def test_stopwords_do_not_overwhelm_semantic_matches(self):
        self.write("lease.txt", "Tenant may keep domestic animals")
        self.write("recipe.txt", "Cook the tomato sauce in the pan")
        self.index(worker_factory=FakeWorker)
        result = search(self.store, "pets in the apartment", worker_factory=FakeWorker)
        self.assertTrue(result[0]["path"].endswith("lease.txt"))

    def test_failure_preserves_old_index_and_retries(self):
        path = self.write("notes.txt", "old searchable contents")
        self.index(keyword_only=True)
        path.write_bytes(b"binary\0contents")
        self.assertEqual(len(self.index(keyword_only=True)["errors"]), 1)
        self.assertEqual(len(search(self.store, "old", keyword_only=True)), 1)
        self.assertEqual(len(self.index(keyword_only=True)["errors"]), 1)
        path.write_text("new updated contents")
        self.assertEqual(self.index(keyword_only=True)["indexed"], 1)
        self.assertEqual(self.store.status()["failures"], [])
        self.assertEqual(search(self.store, "old", keyword_only=True), [])

    def test_missing_root_and_traversal_errors_cannot_prune(self):
        self.write("keep.txt", "keep")
        self.index(keyword_only=True)
        with self.assertRaises(DigupError):
            index(self.store, [self.base / "missing"], keyword_only=True)
        real_walk = os.walk

        def failed_walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("cannot traverse"))
            yield from real_walk(self.base / "missing")

        with patch("diguplinux.crawl.os.walk", failed_walk):
            self.assertTrue(self.index(keyword_only=True)["errors"])
        self.assertEqual(self.store.status()["files"], 1)

    def test_secrets_symlinks_exclusions_and_unicode(self):
        allowed = self.write("árbol 雨.txt", "Bengali বাংলা invoice 12345")
        self.write(".env", "secret")
        self.write("private.pem", "private")
        self.write("credentials.json", '{"password":"secret"}')
        self.write("hidden/.secret.txt", "secret")
        self.write("node_modules/pkg/readme.txt", "vendor")
        self.write("skip/readme.txt", "excluded")
        self.write(
            "key-in-note.txt", "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----"
        )
        outside = self.base / "outside.txt"
        outside.write_text("symlink secret")
        (self.root / "linked.txt").symlink_to(outside)
        result = self.index(keyword_only=True, excludes=[self.root / "skip"])
        self.assertEqual(result["indexed"], 1)
        matches = search(self.store, "বাংলা", keyword_only=True)
        self.assertEqual(matches[0]["path"], str(allowed))
        self.assertEqual(search(self.store, "secret", keyword_only=True), [])

    @unittest.skipUnless(shutil.which("git"), "git required")
    def test_git_ignore_and_separate_code_collection(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "ignored.py\n")
        code = self.write("retry.py", "def retry_with_backoff():\n    return 'network retry'\n")
        self.write("ignored.py", "ignored network retry")
        self.write("node_modules/library.py", "vendor retry")
        self.write("README.md", "project readme network")
        self.index(keyword_only=True)
        self.assertEqual(self.store.status()["files"], 0)
        result = self.index(keyword_only=True, code=True)
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(search(self.store, "retry", keyword_only=True), [])
        matches = search(self.store, "code: backoff", keyword_only=True)
        self.assertEqual(matches[0]["path"], str(code))
        self.assertEqual(matches[0]["line"], 1)

    def test_notebook_does_not_index_outputs(self):
        self.write(
            "demo.ipynb",
            json.dumps(
                {
                    "cells": [
                        {
                            "cell_type": "code",
                            "source": ["print('hello notebook')"],
                            "outputs": [{"text": "sensitive output"}],
                        }
                    ]
                }
            ),
        )
        self.index(code=True, keyword_only=True)
        matches = search(self.store, "code: notebook", keyword_only=True)
        self.assertEqual(matches[0]["location"], "cell 1, line 1")
        self.assertEqual(search(self.store, "code: sensitive", keyword_only=True), [])

    def test_model_version_mismatch_is_explicit(self):
        self.write("lease.txt", "Animals")
        self.index(worker_factory=FakeWorker)
        with self.store.db:
            self.store.db.execute(
                "UPDATE files SET pipeline=?", (json.dumps({"version": PIPELINE_VERSION + "old"}),)
            )
        with self.assertRaisesRegex(DigupError, "different embedding pipeline"):
            search(self.store, "pets", worker_factory=FakeWorker)

    def test_search_is_consistent_during_concurrent_index_update(self):
        self.write("lease.txt", "Tenant may keep animals")
        self.index(worker_factory=FakeWorker)
        folder = self.folder

        class ConcurrentWorker(FakeWorker):
            def embed(self, kind, value):
                writer = Store(folder)
                try:
                    with writer.db:
                        writer.db.execute("DELETE FROM files")
                finally:
                    writer.close()
                return super().embed(kind, value)

        matches = search(self.store, "animals", worker_factory=ConcurrentWorker)
        self.assertEqual(len(matches), 1)
        self.assertEqual(self.store.status()["files"], 0)

    def test_partial_extraction_is_atomic(self):
        path = self.write("notes.txt", "keep these notes")
        self.index(keyword_only=True)
        path.write_text("different notes")

        def fail(*args):
            yield next(chunks("partial index"))
            raise DigupError("failed later")

        with patch("diguplinux.indexer.extract", fail):
            self.assertTrue(self.index(keyword_only=True)["errors"])
        self.assertEqual(len(search(self.store, "keep", keyword_only=True)), 1)
        self.assertEqual(search(self.store, "partial", keyword_only=True), [])

    def test_cli_json_watch_and_null_paths(self):
        path = self.write("invoice.txt", "invoice 54321")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["index", str(self.root), "--index", str(self.folder), "--keyword-only"]), 0
            )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = main(["watch", "--index", str(self.folder), "--once", "--json"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["unchanged"], 1)
        path.write_text("changed invoice 77777")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = main(["--index", str(self.folder), "watch", "--once", "--json"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["indexed"], 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["search", "invoice", "--index", str(self.folder), "--keyword-only", "-0"])
        self.assertEqual(output.getvalue(), str(path) + "\0")

    def test_lock_prevents_concurrent_writes(self):
        with lock(self.folder / ".index.lock"):
            with self.assertRaisesRegex(DigupError, "Another operation"):
                self.index(keyword_only=True)

    def test_verbose_progress_precedes_embedding_and_reports_backend(self):
        self.write("notes.txt", "notes to embed")
        output = io.StringIO()

        class ObservedWorker(FakeWorker):
            info = {"backend": "vulkan", "gpu": True}

            def embed(self, kind, value):
                self_test.assertIn("extracted; embedding", output.getvalue())
                return super().embed(kind, value)

        self_test = self
        with contextlib.redirect_stderr(output):
            result = index(self.store, [self.root], worker_factory=ObservedWorker, verbose=True)
        self.assertEqual(result["indexed"], 1)
        self.assertIn('"backend": "vulkan"', output.getvalue())
        self.assertIn("File 1/1", output.getvalue())
        self.assertIn("1 segments saved", output.getvalue())

    def test_verbose_cli_preserves_json_and_works_with_saved_folders(self):
        self.write("notes.txt", "notes")
        common = ["--index", str(self.folder), "--json", "--verbose"]
        for options in ([str(self.root), "--keyword-only"], ["--reindex"], []):
            output, progress = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
                result = main(["index", *options, *common])
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["indexed"] + report["unchanged"], 1)
            self.assertIn("Scan complete:", progress.getvalue())
            self.assertIn("Indexing complete", progress.getvalue())

    def test_default_output_has_no_verbose_details(self):
        self.write("notes.txt", "notes")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            index(self.store, [self.root], keyword_only=True)
        self.assertIn("Indexed", output.getvalue())
        self.assertNotIn("Segment", output.getvalue())
        self.assertNotIn("Scanning", output.getvalue())
        self.assertNotIn("% complete", output.getvalue())

    def test_verbose_percentage_counts_successes_and_failed_attempts(self):
        self.write("a.txt", "valid text")
        self.write("b.txt", "binary\0contents")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = index(self.store, [self.root], keyword_only=True, verbose=True)
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(len(result["errors"]), 1)
        percentages = [
            line.split("Indexing progress: ")[1]
            for line in output.getvalue().splitlines()
            if "Indexing progress: " in line
        ]
        self.assertEqual(
            percentages,
            [
                "0% complete (0/2 files processed)",
                "50% complete (1/2 files processed)",
                "100% complete (2/2 files processed)",
            ],
        )

    def test_verbose_percentage_for_no_changes(self):
        self.write("notes.txt", "notes")
        self.index(keyword_only=True)
        self.write("unsupported.xyz", "unsupported")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = index(self.store, [self.root], keyword_only=True, verbose=True)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("100% complete (0/0 files processed)", output.getvalue())

    def test_readonly_commands_do_not_create_an_index(self):
        output = io.StringIO()
        folder = self.base / "does-not-exist"
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = main(["status", "--index", str(folder), "--json"])
        self.assertEqual(result, 1)
        self.assertIn("No index", json.loads(output.getvalue())["error"])
        self.assertFalse(folder.exists())


class ChunkTests(unittest.TestCase):
    def test_long_lines_are_bounded_with_correct_locations(self):
        segments = list(chunks("A" * 5000 + "\n" + "B" * 800, size=1000))
        self.assertTrue(all(len(s.text) <= 1000 for s in segments))
        self.assertTrue(all(s.line == 1 for s in segments if "A" in s.text))
        self.assertEqual(segments[-1].line, 2)


if __name__ == "__main__":
    unittest.main()
