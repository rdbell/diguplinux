"""Opt-in checks against the real local model; no network or personal files."""

import contextlib
import io
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fixtures import landscape, pdf

from diguplinux.extract import run
from diguplinux.indexer import index
from diguplinux.runtime import DigupError, Worker
from diguplinux.search import search
from diguplinux.store import Store


@unittest.skipUnless(os.environ.get("DIGUP_INTEGRATION") == "1", "set DIGUP_INTEGRATION=1 after setup")
class ModelTests(unittest.TestCase):
    def test_text_embeddings_and_error_recovery(self):
        worker = Worker(media=True)
        try:
            query = worker.embed("text", "task: search result | query: pets in a rental agreement")
            related = worker.embed(
                "text", "title: lease | text: Tenants may keep domestic animals in the apartment."
            )
            unrelated = worker.embed(
                "text", "title: recipe | text: Cook tomatoes in a pan and simmer the sauce."
            )
            self.assertEqual(len(query), 768)
            self.assertAlmostEqual(math.sqrt(sum(x * x for x in query)), 1, places=5)
            self.assertGreater(
                sum(a * b for a, b in zip(query, related)), sum(a * b for a, b in zip(query, unrelated))
            )
            with self.assertRaises(DigupError):
                worker.embed("image", "/nonexistent/digup-test-image.jpg")
            self.assertEqual(len(worker.embed("text", "task: search result | query: recovery")), 768)
        finally:
            worker.close()
            worker.close()

    @unittest.skipUnless(
        all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "pdftotext", "pdftoppm", "tesseract")),
        "file extraction tools required",
    )
    def test_multimodal_index_search_locations_and_incrementality(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "files"
            root.mkdir()
            (root / "lease.txt").write_text("Tenants may keep domestic animals in the apartment.")
            (root / "recipe.txt").write_text("Cook tomatoes in a pan and simmer the sauce.")
            pdf(
                root / "agreement.pdf",
                ["Building maintenance schedule.", "Tenants may keep pets. Invoice PDF-65432."],
            )
            pdf(base / "screenshot.pdf", ["Invoice OCR-45678. Payment declined."])
            run(
                [
                    "pdftoppm",
                    "-singlefile",
                    "-scale-to",
                    "1024",
                    "-png",
                    str(base / "screenshot.pdf"),
                    str(root / "screen"),
                ]
            )
            landscape(base / "landscape.ppm")
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(base / "landscape.ppm"),
                    "-frames:v",
                    "1",
                    str(root / "landscape.jpg"),
                ]
            )
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-loop",
                    "1",
                    "-i",
                    str(root / "landscape.jpg"),
                    "-t",
                    "7",
                    "-r",
                    "2",
                    "-pix_fmt",
                    "yuv420p",
                    str(root / "video.mp4"),
                ]
            )
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=220:duration=35",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(root / "sound.wav"),
                ]
            )
            store = Store(base / "index")
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    report = index(store, [root])
                self.assertEqual(report["errors"], [])
                self.assertEqual(report["indexed"], 7)
                results = search(store, "a tree under the sun on green hills")
                # The video contains the same landscape; either visual representation can rank first.
                self.assertIn(Path(results[0]["path"]).name, ["landscape.jpg", "video.mp4"])
                self.assertFalse(results[0]["keyword_match"])
                results = search(store, "a tree under the sun on green hills", kinds=["image"])
                self.assertEqual(Path(results[0]["path"]).name, "landscape.jpg")
                results = search(store, "PDF-65432", kinds=["pdf"], keyword_only=True)
                self.assertEqual(results[0]["page"], 2)
                results = search(store, "OCR-45678", kinds=["image"], keyword_only=True)
                self.assertEqual(Path(results[0]["path"]).name, "screen.png")
                results = search(store, "green hills under a sunny sky", kinds=["video"])
                self.assertIn(results[0]["seconds"], [0, 5])
                results = search(store, "a steady musical tone", kinds=["audio"])
                self.assertIn(results[0]["seconds"], [0, 20])
                self.assertEqual(index(store, [root])["unchanged"], 7)
                code = base / "code"
                code.mkdir()
                (code / "retry.py").write_text(
                    "import time\n\ndef reconnect(operation):\n"
                    "    for attempt in range(5):\n        try:\n            return operation()\n"
                    "        except ConnectionError:\n            time.sleep(2 ** attempt)\n"
                )
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(index(store, [code], code=True)["errors"], [])
                results = search(store, "code: retry a failed call with exponential backoff")
                self.assertEqual(Path(results[0]["path"]).name, "retry.py")
                self.assertEqual(results[0]["line"], 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
