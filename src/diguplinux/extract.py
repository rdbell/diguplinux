"""Linux file readers. Temporary media stays alive while segments are consumed."""

import json
import math
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from .runtime import DigupError


@dataclass
class Segment:
    text: str = ""
    location: str = ""
    line: int | None = None
    page: int | None = None
    seconds: float | None = None
    media: str | None = None
    media_type: str | None = None


def run(args: list[str], timeout: int = 120) -> str:
    if not shutil.which(args[0]):
        raise DigupError(f"Missing {args[0]}; run `digup doctor` for dependencies")
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=True)
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as exc:
        raise DigupError(f"{args[0]}: {exc.stderr.decode(errors='replace')[-500:].strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DigupError(f"{args[0]} exceeded {timeout}s timeout") from exc


def chunks(text: str, size: int = 1800):
    """Line-based chunks with bounded long lines and overlap, preserving source lines."""
    lines = text.splitlines(keepends=True)
    current, length, start, previous_line = [], 0, 1, 1
    for number, line in enumerate(lines, 1):
        for offset in range(0, max(1, len(line)), size):
            piece = line[offset : offset + size]
            if current and length + len(piece) > size:
                yield Segment("".join(current).strip(), f"line {start}", line=start)
                overlap = current[-1] if len(current[-1]) < size // 3 and current[-1].strip() else ""
                current = [overlap] if overlap else []
                length = len(overlap)
                start = previous_line if overlap else number
            if not current:
                if not piece.strip():
                    continue
                start = number
            current.append(piece)
            length += len(piece)
            previous_line = number
    if current and "".join(current).strip():
        yield Segment("".join(current).strip(), f"line {start}", line=start)


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def read_text(path: Path) -> str:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise DigupError("Text file exceeds the 20 MiB extraction limit")
    raw = path.read_bytes()
    if b"\0" in raw[:8192]:
        raise DigupError("File contains binary data")
    text = raw.decode("utf-8-sig")
    if "-----BEGIN " in text and re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        raise DigupError("Skipped file containing a private key")
    return text


def document(path: Path):
    ext = path.suffix.lower()
    if ext in {".docx", ".odt"}:
        with zipfile.ZipFile(path) as archive:
            member = "word/document.xml" if ext == ".docx" else "content.xml"
            info = archive.getinfo(member)
            if info.file_size > 20 * 1024 * 1024:
                raise DigupError("Expanded document exceeds 20 MiB")
            root = ET.fromstring(archive.read(member))
        paragraphs = [
            "".join(element.itertext())
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] in {"p", "h"}
        ]
        return "\n".join(paragraphs)
    text = read_text(path)
    if ext in {".html", ".htm"}:
        parser = HTMLText()
        parser.feed(text)
        return "".join(parser.parts)
    return text


def ocr(path: Path, language: str) -> str:
    return run(["tesseract", str(path), "stdout", "-l", language], timeout=90).strip()


def frame(path: Path, output: Path, seconds: float | None = None):
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if seconds is not None:
        args += ["-ss", str(seconds)]
    args += [
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=1024:1024:force_original_aspect_ratio=decrease",
        str(output),
    ]
    run(args)


def extract(
    path: Path,
    kind: str,
    semantic: bool,
    language: str = "eng",
    max_segments: int = 5000,
    video_interval: float = 5,
    duration_limit: float | None = None,
):
    with tempfile.TemporaryDirectory(prefix="digup-") as temporary:
        work = Path(temporary)
        if kind in {"doc", "code"}:
            if path.suffix.lower() == ".ipynb":
                notebook = json.loads(read_text(path))
                for i, cell in enumerate(notebook.get("cells", []), 1):
                    source = cell.get("source", [])
                    source = source if isinstance(source, str) else "".join(source)
                    for segment in chunks(source):
                        segment.location = f"cell {i}, line {segment.line}"
                        yield segment
            else:
                yield from chunks(document(path))
        elif kind == "image":
            image = work / "image.jpg"
            frame(path, image)
            text = ocr(image, language) if shutil.which("tesseract") else ""
            yield Segment(text, "image", media=str(image) if semantic else None, media_type="image")
        elif kind == "pdf":
            text = run(["pdftotext", "-layout", str(path), "-"], timeout=180)
            pages = text.split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
            if len(pages) > max_segments:
                raise DigupError(f"PDF exceeds {max_segments} pages; increase --max-segments")
            for number, text in enumerate(pages, 1):
                image = None
                if semantic or not text.strip():
                    prefix = work / "page"
                    run(
                        [
                            "pdftoppm",
                            "-f",
                            str(number),
                            "-l",
                            str(number),
                            "-singlefile",
                            "-scale-to",
                            "1024",
                            "-jpeg",
                            str(path),
                            str(prefix),
                        ]
                    )
                    image = str(prefix) + ".jpg"
                if not text.strip() and image:
                    text = ocr(Path(image), language)
                if semantic:
                    yield Segment(
                        text[:12000], f"page {number}", page=number, media=image, media_type="image"
                    )
                else:
                    yield Segment(text[:12000], f"page {number}", page=number)
                # Long pages also get text chunks so passages are not truncated out of meaning search.
                if semantic and text.strip():
                    for segment in chunks(text):
                        segment.location, segment.page, segment.line = f"page {number}", number, None
                        yield segment
        elif kind in {"audio", "video"}:
            if not semantic:
                yield Segment("", "file (content needs semantic indexing)")
                return
            probe = json.loads(
                run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
            )
            duration = float(probe.get("format", {}).get("duration", 0))
            if not math.isfinite(duration) or duration <= 0:
                raise DigupError("Cannot determine media duration")
            if duration_limit is not None:
                # Benchmark sampling: cap window/frame start times while reading the original file.
                if not math.isfinite(duration_limit) or duration_limit <= 0:
                    raise DigupError("Media sampling limit must be finite and positive")
                duration = min(duration, duration_limit)
            has_audio = any(s.get("codec_type") == "audio" for s in probe.get("streams", []))
            count = math.ceil(duration / 20) if has_audio else 0
            count += math.ceil(duration / video_interval) if kind == "video" else 0
            if count > max_segments:
                raise DigupError(f"Media exceeds {max_segments} windows/frames; increase --max-segments")
            if has_audio:
                for start in range(0, math.ceil(duration), 20):
                    audio = work / "audio.wav"
                    run(
                        [
                            "ffmpeg",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-nostdin",
                            "-y",
                            "-ss",
                            str(start),
                            "-i",
                            str(path),
                            "-t",
                            "30",
                            "-vn",
                            "-ac",
                            "1",
                            "-ar",
                            "16000",
                            str(audio),
                        ]
                    )
                    yield Segment(
                        "", f"{start}s (audio)", seconds=float(start), media=str(audio), media_type="audio"
                    )
            if kind == "video":
                for i in range(math.ceil(duration / video_interval)):
                    start = i * video_interval
                    image = work / "frame.jpg"
                    frame(path, image, start)
                    yield Segment(
                        "", f"{start:g}s (frame)", seconds=start, media=str(image), media_type="image"
                    )
