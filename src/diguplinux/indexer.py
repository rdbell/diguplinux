import json
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache
from pathlib import Path

from .config import PIPELINE_VERSION
from .crawl import scan, within
from .extract import Segment, extract
from .runtime import DigupError, Worker, lock


@lru_cache(maxsize=128)
def cache_key(pipeline):
    """Compare extraction settings, including records written before selection was separated."""
    try:
        value = json.loads(pipeline)
        if not isinstance(value, dict) or not isinstance(value.get("options"), dict):
            return None
        for name in ("excludes", "exclude_ext", "ignore_patterns"):
            value["options"].pop(name, None)
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return None


def index(
    store,
    roots,
    code=False,
    keyword_only=False,
    excludes=(),
    exclude_ext=frozenset(),
    language="eng",
    max_segments=5000,
    video_interval=5,
    reindex=False,
    worker_factory=Worker,
    verbose=False,
    ignore_patterns=(),
):
    started = time.monotonic()

    def progress(message):
        if verbose:
            print(f"[{time.monotonic() - started:.1f}s] {message}", file=sys.stderr, flush=True)

    collection = "code" if code else "files"
    options = dict(
        keyword_only=keyword_only,
        language=language,
        max_segments=max_segments,
        video_interval=video_interval,
    )
    pipeline = json.dumps({"version": PIPELINE_VERSION, "options": options}, sort_keys=True)
    report = dict(indexed=0, unchanged=0, removed=0, skipped=0, errors=[])
    worker = None
    with lock(store.folder / ".index.lock"):
        progress(f"Scanning {len(roots)} folder(s) ({collection})")
        scanned = scan(roots, code, excludes, exclude_ext, ignore_patterns)
        report["skipped"] = scanned.skipped
        report["errors"].extend(scanned.errors)
        existing = store.existing(collection)
        changed = []
        for path, kind in scanned.files.items():
            try:
                stat = path.stat()
            except OSError as exc:
                report["errors"].append(f"{path}: {exc}")
                continue
            old = existing.get(path)
            if (
                not reindex
                and old
                and old["mtime_ns"] == stat.st_mtime_ns
                and old["size"] == stat.st_size
                and cache_key(old["pipeline"]) == pipeline
            ):
                report["unchanged"] += 1
            else:
                changed.append((path, kind, stat))
        progress(
            f"Scan complete: {len(changed)} to index, {report['unchanged']} unchanged, "
            f"{report['skipped']} skipped, {len(report['errors'])} errors"
        )

        def file_progress(processed):
            percent = processed * 100 // len(changed) if changed else 100
            progress(f"Indexing progress: {percent}% complete ({processed}/{len(changed)} files processed)")

        file_progress(0)
        try:
            if changed and not keyword_only:
                progress("Loading embedding worker")
                worker = worker_factory(
                    media=any(kind in {"image", "pdf", "audio", "video"} for _, kind, _ in changed)
                )
                progress(f"Embedding worker ready: {json.dumps(getattr(worker, 'info', {}))}")
            elif changed:
                progress("Keyword-only indexing")
            for number, (path, kind, stat) in enumerate(changed, 1):
                progress(f"File {number}/{len(changed)}: {path} ({kind}, {stat.st_size} bytes); extracting")
                try:
                    segments = []
                    for segment in extract(
                        path, kind, not keyword_only, language, max_segments, video_interval
                    ):
                        if len(segments) >= max_segments:
                            raise DigupError(f"File exceeds {max_segments} segments; increase --max-segments")
                        vector = None
                        label = f"Segment {len(segments) + 1} ({segment.location or 'file'})"
                        progress(f"{label}: extracted; {'embedding' if worker else 'storing text'}")
                        if worker:
                            if segment.media:
                                vector = worker.embed(segment.media_type, segment.media)
                            elif segment.text:
                                vector = worker.embed("text", f"title: {path.name} | text: {segment.text}")
                        segments.append((segment, vector))
                        progress(f"{label}: complete")
                    if not segments:
                        segments.append((Segment("", "file"), None))
                    after = path.stat()
                    if after.st_mtime_ns != stat.st_mtime_ns or after.st_size != stat.st_size:
                        raise DigupError("File changed while indexing; retry on the next pass")
                    store.replace(path, collection, kind, stat, pipeline, segments)
                    report["indexed"] += 1
                    progress(f"File {number}/{len(changed)} complete: {len(segments)} segments saved")
                    print(f"Indexed {path}", file=sys.stderr)
                except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile, DigupError) as exc:
                    error = str(exc)
                    store.failure(path, collection, error)
                    report["errors"].append(f"{path}: {error}")
                    print(f"Skipped {path}: {error}", file=sys.stderr)
                file_progress(number)
            # A failed traversal must never erase unvisited files. Extraction failures keep their old records.
            if not scanned.errors:
                with store.db:
                    for path, old in existing.items():
                        if any(within(path, root) for root in roots) and path not in scanned.files:
                            store.db.execute("DELETE FROM files WHERE id=?", (old["id"],))
                            report["removed"] += 1
                            progress(f"Removed deleted file: {path}")
                    for row in store.db.execute(
                        "SELECT path FROM failures WHERE collection=?", (collection,)
                    ).fetchall():
                        path = Path(row[0])
                        if any(within(path, root) for root in roots) and path not in scanned.files:
                            store.db.execute(
                                "DELETE FROM failures WHERE path=? AND collection=?", (row[0], collection)
                            )
        finally:
            if worker:
                worker.close()
    progress("Indexing complete")
    return report
