import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diguplinux.cli import main, parser
from diguplinux.config import config_path
from diguplinux.indexer import index
from diguplinux.runtime import DigupError, lock
from diguplinux.settings import Settings
from diguplinux.store import Store


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "documents"
        self.root.mkdir()
        (self.root / "invoice.txt").write_text("Invoice 65432. Payment declined.")
        self.index_path = self.base / "index"
        self.config = self.base / "config/config.toml"
        self.settings = Settings(self.config)

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = main(["--config", str(self.config), "--index", str(self.index_path), *args, "--json"])
        return result, json.loads(output.getvalue()), errors.getvalue()

    def initial_index(self, *options):
        result, report, _ = self.invoke("index", str(self.root), "--keyword-only", *options)
        self.assertEqual(result, 0, report)
        return report

    def test_cli_saves_complete_folder_options_separate_from_database(self):
        self.initial_index(
            "--exclude",
            str(self.root / "private"),
            "--exclude-ext",
            "csv,log",
            "--ocr-language",
            "eng+spa",
            "--max-segments",
            "123",
            "--video-interval",
            "7.5",
        )
        folders = self.settings.folders(self.index_path)
        self.assertEqual(len(folders), 1)
        self.assertEqual(
            folders[0],
            {
                "path": str(self.root),
                "collection": "files",
                "options": {
                    "keyword_only": True,
                    "excludes": [str(self.root / "private")],
                    "exclude_ext": [".csv", ".log"],
                    "ignore_patterns": [],
                    "language": "eng+spa",
                    "max_segments": 123,
                    "video_interval": 7.5,
                },
            },
        )
        store = Store(self.index_path, readonly=True)
        try:
            self.assertIsNone(
                store.db.execute("SELECT name FROM sqlite_master WHERE name='roots'").fetchone()
            )
        finally:
            store.close()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)
        self.assertFalse(list(self.config.parent.glob("*.tmp")))

    def test_ignore_patterns_persist_prune_and_rebuild(self):
        self.initial_index()
        (self.root / "drafts").mkdir()
        (self.root / "drafts/notes.txt").write_text("draft")
        (self.root / "scratch.txt").write_text("scratch")
        report = self.initial_index("--ignore-pattern", "**/drafts/**", "--ignore-pattern", "invoice.*")
        self.assertEqual(report["removed"], 1)
        self.assertEqual(report["indexed"], 1)
        self.assertEqual(
            self.settings.folders(self.index_path)[0]["options"]["ignore_patterns"],
            ["**/drafts/**", "invoice.*"],
        )
        result, report, _ = self.invoke("watch", "--once")
        self.assertEqual(result, 0)
        self.assertEqual(report["unchanged"], 1)
        self.index_path.rename(self.base / "old-index")
        result, report, _ = self.invoke("index")
        self.assertEqual(result, 0)
        self.assertEqual(report["indexed"], 1)

    def test_invalid_ignore_pattern_does_not_save_or_index(self):
        result, report, _ = self.invoke("index", str(self.root), "--ignore-pattern", "../private")
        self.assertEqual(result, 1)
        self.assertIn("relative globs", report["error"])
        self.assertFalse(self.config.exists())
        self.assertFalse(self.index_path.exists())

    def test_missing_database_rebuilds_using_config_and_saved_exclusions(self):
        (self.root / "private.txt").write_text("Secret excluded content")
        self.initial_index("--exclude", str(self.root / "private.txt"))
        saved = self.config.read_bytes()
        self.index_path.rename(self.base / "old-index")
        result, report, _ = self.invoke("index")
        self.assertEqual(result, 0, report)
        self.assertEqual(report["indexed"], 1)
        self.assertEqual(self.config.read_bytes(), saved)
        result, matches, _ = self.invoke("search", "Secret", "--keyword-only")
        self.assertEqual(result, 0)
        self.assertEqual(matches, [])

    def test_watch_recovers_missing_database_with_normal_and_code_folders(self):
        self.initial_index()
        code = self.base / "code"
        code.mkdir()
        (code / "retry.py").write_text("def retry_with_backoff():\n    pass\n")
        result, report, _ = self.invoke("index", str(code), "--code", "--keyword-only")
        self.assertEqual(result, 0, report)
        self.index_path.rename(self.base / "backup-index")
        result, report, _ = self.invoke("watch", "--once")
        self.assertEqual(result, 0, report)
        self.assertEqual(report["indexed"], 2)
        result, matches, _ = self.invoke("search", "code: backoff", "--keyword-only")
        self.assertEqual(result, 0)
        self.assertEqual(matches[0]["path"], str(code / "retry.py"))

    def test_corruption_leaves_config_and_database_intact_until_explicit_recovery(self):
        self.initial_index()
        saved = self.config.read_bytes()
        database = self.index_path / "index.sqlite"
        damaged = b"This is a deliberately corrupted test database."
        database.write_bytes(damaged)
        result, config, _ = self.invoke("config")
        self.assertEqual(result, 0)
        self.assertEqual(config["folders"][0]["path"], str(self.root))
        result, report, _ = self.invoke("index")
        self.assertEqual(result, 1)
        self.assertIn("database", report["error"])
        self.assertEqual(database.read_bytes(), damaged)
        self.assertEqual(self.config.read_bytes(), saved)
        self.index_path.rename(self.base / "index-corrupt")
        result, report, _ = self.invoke("index")
        self.assertEqual(result, 0, report)
        self.assertEqual(report["indexed"], 1)
        self.assertEqual((self.base / "index-corrupt/index.sqlite").read_bytes(), damaged)

    def test_legacy_folder_list_is_imported_once_without_overwriting_config(self):
        store = Store(self.index_path)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                index(store, [self.root], keyword_only=True)
            legacy_options = {
                "keyword_only": True,
                "excludes": [],
                "exclude_ext": [],
                "language": "eng",
                "max_segments": 5000,
                "video_interval": 5,
            }
            with store.db:
                store.db.execute("CREATE TABLE roots(path TEXT,collection TEXT,options TEXT)")
                store.db.execute(
                    "INSERT INTO roots VALUES (?,?,?)", (str(self.root), "files", json.dumps(legacy_options))
                )
        finally:
            store.close()
        result, report, _ = self.invoke("watch", "--once")
        self.assertEqual(result, 0, report)
        self.assertEqual(report["unchanged"], 1)
        self.assertEqual(
            self.settings.folders(self.index_path)[0]["options"], legacy_options | {"ignore_patterns": []}
        )
        # An explicitly empty folder list stays authoritative despite the stale legacy roots table.
        self.config.write_text(
            f"version = 1\n[[indexes]]\npath = {json.dumps(str(self.index_path))}\nfolders = []\n"
        )
        before = self.config.read_bytes()
        result, report, _ = self.invoke("watch", "--once")
        self.assertEqual(result, 1)
        self.assertIn("No saved folders", report["error"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_existing_config_wins_over_legacy_database_settings(self):
        self.initial_index()
        store = Store(self.index_path)
        try:
            with store.db:
                store.db.execute("CREATE TABLE roots(path TEXT,collection TEXT,options TEXT)")
                store.db.execute(
                    "INSERT INTO roots VALUES (?,?,?)", (str(self.base / "wrong-folder"), "files", "{}")
                )
        finally:
            store.close()
        result, report, _ = self.invoke("index")
        self.assertEqual(result, 0, report)
        self.assertEqual(report["unchanged"], 1)
        self.assertEqual([r["path"] for r in self.settings.folders(self.index_path)], [str(self.root)])

    def test_alternate_indexes_have_independent_folder_lists(self):
        self.initial_index()
        other_root = self.base / "other"
        other_root.mkdir()
        (other_root / "other.txt").write_text("Different folder")
        other_index = self.base / "other-index"
        result, report, _ = self.invoke(
            "index", str(other_root), "--index", str(other_index), "--keyword-only"
        )
        self.assertEqual(result, 0, report)
        self.assertEqual([r["path"] for r in self.settings.folders(self.index_path)], [str(self.root)])
        self.assertEqual([r["path"] for r in self.settings.folders(other_index)], [str(other_root)])

    def test_invalid_config_is_not_overwritten_and_prevents_index_changes(self):
        self.initial_index()
        database = self.index_path / "index.sqlite"
        before = database.read_bytes()
        broken = b"version = 1\n[[indexes]\n"
        self.config.write_bytes(broken)
        result, report, _ = self.invoke("index", str(self.root), "--keyword-only")
        self.assertEqual(result, 1)
        self.assertIn("Invalid config", report["error"])
        self.assertEqual(self.config.read_bytes(), broken)
        self.assertEqual(database.read_bytes(), before)

    def test_config_is_saved_before_inference_or_extraction_can_fail(self):
        with patch("diguplinux.cli.index", side_effect=DigupError("missing model")):
            result, report, _ = self.invoke("index", str(self.root))
        self.assertEqual(result, 1)
        self.assertEqual(report["error"], "missing model")
        self.assertEqual(self.settings.folders(self.index_path)[0]["path"], str(self.root))

    def test_failed_atomic_save_keeps_prior_config_and_cleans_temporary_file(self):
        self.initial_index()
        before = self.config.read_bytes()
        extra = self.base / "extra"
        extra.mkdir()
        with patch("diguplinux.settings.os.fsync", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.settings.remember(self.index_path, [extra])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.config.parent.glob("*.tmp")), [])

    def test_lock_and_fresh_reads_prevent_lost_updates(self):
        second = Settings(self.config)
        second.read()
        self.initial_index()
        extra = self.base / "extra"
        extra.mkdir()
        second.remember(self.index_path, [extra])
        self.assertEqual(len(self.settings.folders(self.index_path)), 2)
        before = self.config.read_bytes()
        with lock(self.config.with_name("config.toml.lock")):
            with self.assertRaisesRegex(DigupError, "Another operation"):
                self.settings.remember(self.index_path, [self.root])
        self.assertEqual(self.config.read_bytes(), before)

    def test_dotfile_symlink_and_unicode_paths_survive_atomic_save(self):
        target = self.base / "dotfiles/digup.toml"
        target.parent.mkdir()
        self.config.parent.mkdir()
        self.config.symlink_to(target)
        odd = self.base / '雨 🌳 "quoted" \\folder\nwith newline'
        odd.mkdir()
        Settings(self.config).remember(self.index_path, [odd])
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(Settings(self.config).folders(self.index_path)[0]["path"], str(odd))

    def test_missing_root_remains_configured_for_future_retry(self):
        self.initial_index()
        before = self.config.read_bytes()
        self.root.rename(self.base / "unmounted")
        result, report, _ = self.invoke("index")
        self.assertEqual(result, 1)
        self.assertTrue(report["errors"])
        self.assertEqual(self.config.read_bytes(), before)
        store = Store(self.index_path, readonly=True)
        try:
            self.assertEqual(store.status()["files"], 1)
        finally:
            store.close()

    def test_unknown_or_invalid_options_are_rejected_before_save(self):
        for setting in (
            "collection = []",
            'keyword_only = "yes"',
            "max_segments = 0",
            "video_interval = nan",
            'excludes = ["relative"]',
            "unexpected = true",
        ):
            with self.subTest(setting=setting):
                self.config.parent.mkdir(exist_ok=True)
                self.config.write_text(
                    f"version = 1\n[[indexes]]\npath = {json.dumps(str(self.index_path))}\n"
                    f"[[indexes.folders]]\npath = {json.dumps(str(self.root))}\n{setting}\n"
                )
                before = self.config.read_bytes()
                with self.assertRaises(DigupError):
                    self.settings.remember(self.index_path, [self.root])
                self.assertEqual(self.config.read_bytes(), before)

    def test_config_inspection_is_read_only_without_database(self):
        result, config, _ = self.invoke("config")
        self.assertEqual(result, 0)
        self.assertEqual(config["folders"], [])
        self.assertFalse(self.config.parent.exists())
        self.assertFalse(self.index_path.exists())

    def test_watch_reloads_manual_settings_between_passes(self):
        self.initial_index()
        calls = []

        def next_pass(interval):
            calls.append(interval)
            if len(calls) == 1:
                self.settings.remember(
                    self.index_path,
                    [self.root],
                    options={
                        "keyword_only": True,
                        "excludes": [str(self.root / "invoice.txt")],
                    },
                )
            else:
                raise KeyboardInterrupt

        with patch("diguplinux.cli.time.sleep", side_effect=next_pass):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = main(["watch", "--config", str(self.config), "--index", str(self.index_path)])
        self.assertEqual(result, 130)
        store = Store(self.index_path, readonly=True)
        try:
            self.assertEqual(store.status()["files"], 0)
        finally:
            store.close()

    def test_xdg_config_and_explicit_override_before_or_after_command(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "xdg")}):
            self.assertEqual(config_path(), self.base / "xdg/diguplinux/config.toml")
            self.assertEqual(parser().parse_args(["config"]).config, config_path())
            for args in (["--config", str(self.config), "config"], ["config", "--config", str(self.config)]):
                self.assertEqual(parser().parse_args(args).config, self.config)
        for value in ("", "relative"):
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": value}):
                self.assertEqual(config_path(), Path.home() / ".config/diguplinux/config.toml")


if __name__ == "__main__":
    unittest.main()
