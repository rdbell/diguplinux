import shutil
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from fixtures import pdf

from diguplinux.extract import document, extract, run
from diguplinux.runtime import DigupError


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_html_discards_scripts_and_styles(self):
        path = self.root / "document.html"
        path.write_text(
            "<style>secret style</style><p>Visible &amp; readable</p>"
            "<script>secret code</script><p>Next paragraph</p>"
        )
        text = document(path)
        self.assertIn("Visible & readable", text)
        self.assertIn("Next paragraph", text)
        self.assertNotIn("secret", text)

    def test_docx_and_odt_text(self):
        for ext, member, xml in [
            ("docx", "word/document.xml", "<document><p><r><t>Invoice 65432</t></r></p></document>"),
            ("odt", "content.xml", "<document><p>Invoice 65432</p></document>"),
        ]:
            path = self.root / ("invoice." + ext)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(member, xml)
            self.assertEqual(document(path), "Invoice 65432")

    @unittest.skipUnless(shutil.which("pdftotext"), "Poppler required")
    def test_pdf_pages_and_limit(self):
        path = self.root / "lease.pdf"
        pdf(path, ["Building maintenance schedule.", "Tenants may keep pets. Invoice PDF-65432."])
        segments = list(extract(path, "pdf", False))
        self.assertEqual([s.page for s in segments], [1, 2])
        self.assertIn("PDF-65432", segments[1].text)
        with self.assertRaisesRegex(DigupError, "exceeds 1 pages"):
            list(extract(path, "pdf", False, max_segments=1))

    @unittest.skipUnless(
        all(shutil.which(t) for t in ("pdftoppm", "tesseract", "ffmpeg")), "OCR tools required"
    )
    def test_image_ocr(self):
        source = self.root / "invoice.pdf"
        pdf(source, ["Invoice OCR-45678. Payment declined."])
        run(["pdftoppm", "-singlefile", "-scale-to", "1024", "-png", str(source), str(self.root / "screen")])
        segments = list(extract(self.root / "screen.png", "image", False))
        self.assertIn("OCR-45678", segments[0].text)
        self.assertIn("Payment declined", segments[0].text)

    @unittest.skipUnless(all(shutil.which(t) for t in ("ffmpeg", "ffprobe")), "FFmpeg required")
    def test_overlapping_audio_windows_and_limits(self):
        path = self.root / "sound.wav"
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
                str(path),
            ]
        )
        starts, lengths = [], []
        for segment in extract(path, "audio", True):
            starts.append(segment.seconds)
            with wave.open(segment.media) as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnchannels(), 1)
                lengths.append(audio.getnframes() / audio.getframerate())
        self.assertEqual(starts, [0, 20])
        self.assertEqual(lengths, [30, 15])
        with self.assertRaisesRegex(DigupError, "exceeds 1"):
            list(extract(path, "audio", True, max_segments=1))

    @unittest.skipUnless(all(shutil.which(t) for t in ("ffmpeg", "ffprobe")), "FFmpeg required")
    def test_media_sampling_caps_start_times_without_shortening_audio_windows(self):
        path = self.root / "sound.wav"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:duration=80",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(path),
            ]
        )
        starts, lengths = [], []
        for segment in extract(path, "audio", True, duration_limit=60):
            starts.append(segment.seconds)
            with wave.open(segment.media) as audio:
                lengths.append(audio.getnframes() / audio.getframerate())
        self.assertEqual(starts, [0, 20, 40])
        self.assertEqual(lengths, [30, 30, 30])
        for limit in (0, -1, float("nan"), float("inf")):
            with self.assertRaisesRegex(DigupError, "finite and positive"):
                list(extract(path, "audio", True, duration_limit=limit))
