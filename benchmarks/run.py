"""Full-file, sequential CPU/GPU indexing benchmarks. Inputs are downloaded separately.

Run from the checkout: python benchmarks/run.py --device cpu
Then run --device gpu against the same manifest. This never writes the user's config/index.
"""

import argparse
import json
import math
import os
import resource
import time
from pathlib import Path

from diguplinux import indexer
from diguplinux.config import LLAMA_REVISION, MODEL_REVISION, PIPELINE_VERSION, cache_dir
from diguplinux.runtime import Worker
from diguplinux.store import Store


class Profile:
    def __init__(self, device, case, segment_limit=None, media_seconds=None):
        self.device, self.case = device, case
        self.load_seconds = 0
        self.embedding_seconds = 0
        self.extraction_seconds = 0
        self.counts = {}
        self.info = None
        self.worker_log = ""
        self.started = time.perf_counter()
        self.previous_progress = self.started
        self.segment_limit, self.media_seconds = segment_limit, media_seconds

    def worker(self, media=False):
        start = time.perf_counter()
        delegate = Worker(media=media, device=self.device)
        self.load_seconds += time.perf_counter() - start
        self.info = delegate.info
        owner = self

        class TimedWorker:
            def embed(self, kind, value):
                start = time.perf_counter()
                try:
                    return delegate.embed(kind, value)
                finally:
                    owner.embedding_seconds += time.perf_counter() - start
                    owner.counts[kind] = owner.counts.get(kind, 0) + 1
                    now = time.perf_counter()
                    if now - owner.previous_progress >= 20:
                        print(
                            json.dumps(
                                {
                                    "event": "progress",
                                    "device": owner.device,
                                    "case": owner.case,
                                    "embeddings": sum(owner.counts.values()),
                                    "elapsed_seconds": now - owner.started,
                                }
                            ),
                            flush=True,
                        )
                        owner.previous_progress = now

            def close(self):
                delegate.log.seek(0)
                owner.worker_log = delegate.log.read().decode(errors="replace")
                delegate.close()

        return TimedWorker()

    def extract(self, original, *args):
        generator = original(*args, duration_limit=self.media_seconds)
        yielded = 0
        try:
            while True:
                if self.segment_limit is not None and yielded >= self.segment_limit:
                    return
                start = time.perf_counter()
                try:
                    segment = next(generator)
                except StopIteration:
                    return
                finally:
                    self.extraction_seconds += time.perf_counter() - start
                yielded += 1
                yield segment
        finally:
            generator.close()


def usage():
    parent = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    return parent.ru_utime + parent.ru_stime + child.ru_utime + child.ru_stime


def run_case(case, device, output, segment_limit=None, media_seconds=None):
    source = Path(case["path"])
    roots = [source if source.is_dir() else source.parent]
    profile = Profile(device, case["id"], segment_limit, media_seconds)
    original = indexer.extract
    indexer.extract = lambda *values: profile.extract(original, *values)
    started, cpu_start = time.perf_counter(), usage()
    print(json.dumps({"event": "start", "device": device, "case": case["id"]}), flush=True)
    store = None
    folder = output / device / case["id"]
    try:
        store = Store(folder)
        report = indexer.index(store, roots, worker_factory=profile.worker)
        stats = store.status()
    finally:
        if store:
            store.close()
        indexer.extract = original
    elapsed, cpu_seconds = time.perf_counter() - started, usage() - cpu_start
    result = {
        "case": case["id"],
        "device": device,
        "input_bytes": case["bytes"],
        "wall_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
        "model_load_seconds": profile.load_seconds,
        "embedding_seconds": profile.embedding_seconds,
        "extraction_seconds": profile.extraction_seconds,
        "other_seconds": elapsed
        - profile.load_seconds
        - profile.embedding_seconds
        - profile.extraction_seconds,
        "embedding_counts": profile.counts,
        "worker": profile.info,
        "files": stats["files"],
        "segments": stats["segments"],
        "vectors": stats["vectors"],
        "db_bytes": (folder / "index.sqlite").stat().st_size,
        "report": report,
        "pipeline": PIPELINE_VERSION,
        "model_revision": MODEL_REVISION,
        "llama_revision": LLAMA_REVISION,
        "threads": 8,
        "video_interval": 5,
        "audio_window": 30,
        "audio_hop": 20,
        "scope": "sample" if segment_limit is not None or media_seconds is not None else "full",
        "segment_limit": segment_limit,
        "media_window_start_limit_seconds": media_seconds,
    }
    (folder / "worker.log").write_text(profile.worker_log)
    print(json.dumps({"event": "complete", **result}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=["cpu", "gpu", "both"], required=True)
    parser.add_argument("--manifest", type=Path, default=cache_dir() / "benchmarks/manifest.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", help="run only selected case IDs")
    parser.add_argument("--segment-limit", type=int, help="index the same first N segments on each device")
    parser.add_argument(
        "--media-seconds", type=float, help="sample media windows/frames starting before this time"
    )
    args = parser.parse_args()
    if args.segment_limit is not None and args.segment_limit <= 0:
        parser.error("--segment-limit must be positive")
    if args.media_seconds is not None and (not math.isfinite(args.media_seconds) or args.media_seconds <= 0):
        parser.error("--media-seconds must be finite and positive")
    os.environ["DIGUP_THREADS"] = "8"
    os.environ.pop("DIGUP_GPU_LAYERS", None)
    os.environ.pop("DIGUP_WORKER", None)
    os.environ["GGML_VK_DISABLE_F16"] = "1"
    os.environ["GGML_VK_VISIBLE_DEVICES"] = "0"
    output = args.output or cache_dir() / "benchmarks/runs" / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    cases = json.loads(args.manifest.read_text())
    cases.sort(key=lambda item: item["bytes"])
    results = []
    for case in cases:
        if args.case and case["id"] not in args.case:
            continue
        for device in ["cpu", "gpu"] if args.device == "both" else [args.device]:
            result = run_case(case, device, output, args.segment_limit, args.media_seconds)
            results.append(result)
            (output / "results.json").write_text(json.dumps(results, indent=2))
            if result["report"]["errors"]:
                raise SystemExit("Benchmark case failed; results saved with errors")
    print(f"RESULTS {output / 'results.json'}", flush=True)


if __name__ == "__main__":
    main()
