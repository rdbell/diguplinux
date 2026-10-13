import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("digup_helper", Path(__file__).parents[1] / "bin/search.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.exe = self.base / "fake digup"

    def executable(self, body):
        self.exe.write_text("#!/usr/bin/env python3\n" + body)
        self.exe.chmod(0o700)
        return str(self.exe)

    def test_argv_and_safe_result_paths(self):
        exe = self.executable(
            "import sys,json\n"
            "assert sys.argv[-2:] == ['--', '--query $(touch nope)']\n"
            "assert '--keyword-only' in sys.argv\n"
            "print(json.dumps([{'path': '/tmp/quoted file.txt', 'location': 'page 2', 'snippet': 'hello'},"
            " {'path': 'relative'}, {'path': 'https://example.org'}, {'path': '/bad\\u0000path'}]))\n"
        )
        result = helper.search(exe, "--query $(touch nope)", keyword_only=True)
        self.assertEqual(
            result, {"results": [{"path": "/tmp/quoted file.txt", "location": "page 2", "snippet": "hello"}]}
        )

    def test_errors_and_empty_query(self):
        self.assertEqual(helper.search("missing", ""), {"results": []})
        self.assertIn("not found", helper.search(str(self.base / "missing"), "pets")["error"])
        self.assertIn("cpu or gpu", helper.search("missing", "pets", device="bad")["error"])
        exe = self.executable(
            "import sys,json\nprint(json.dumps({'error': 'No index; run digup index'}))\nsys.exit(1)\n"
        )
        self.assertEqual(helper.search(exe, "pets")["error"], "No index; run digup index")
        self.executable("print('bad json')\n")
        self.assertIn("unreadable", helper.search(exe, "pets")["error"])

    def test_cancellation_stops_child_group(self):
        pidfile = self.base / "pid"
        exe = self.executable(
            "import os,time,pathlib\npathlib.Path("
            + repr(str(pidfile))
            + ").write_text(str(os.getpid()))\ntime.sleep(60)\n"
        )
        proc = subprocess.Popen(
            ["python3", str(Path(helper.__file__)), "--executable", exe, "--query=pets"],
            stdout=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists())
            child = int(pidfile.read_text())
            proc.terminate()
            proc.communicate(timeout=5)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_installed_digup_on_temporary_index(self):
        exe = Path.home() / ".local/bin/digup"
        if not exe.exists():
            self.skipTest("installed digup required")
        root = self.base / "docs"
        root.mkdir()
        path = root / "invoice.txt"
        path.write_text("Invoice 65432 payment declined")
        index = self.base / "index"
        subprocess.run(
            [
                str(exe),
                "index",
                str(root),
                "--keyword-only",
                "--index",
                str(index),
                "--config",
                str(self.base / "config.toml"),
            ],
            check=True,
            capture_output=True,
        )
        result = helper.search(str(exe), "65432", str(index), keyword_only=True)
        self.assertEqual(result["results"][0]["path"], str(path))
        self.assertIn("65432", result["results"][0]["snippet"])
