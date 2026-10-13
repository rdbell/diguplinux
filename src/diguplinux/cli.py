import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import MODEL_FILES, config_path, model_dir, state_dir, worker_path
from .indexer import index
from .runtime import DigupError, build_runtime, device_mode, download_models
from .search import search
from .settings import Settings
from .store import Store


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def positive_float(value):
    result = float(value)
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def absolute(value):
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def parser():
    app = argparse.ArgumentParser(prog="digup", description="Search local files by meaning and exact words.")
    app.add_argument("--version", action="version", version=__version__)
    app.add_argument("--index", type=absolute, default=state_dir() / "index", help="index directory")
    app.add_argument("--config", type=absolute, default=config_path(), help="folder configuration file")
    app.add_argument(
        "--device", choices=["cpu", "gpu"], help="inference device (default: DIGUP_DEVICE or cpu)"
    )
    subcommands = app.add_subparsers(dest="command", required=True)

    def command(name, help_, output=True):
        cmd = subcommands.add_parser(name, help=help_)
        cmd.add_argument("--index", type=absolute, default=argparse.SUPPRESS, help="index directory")
        cmd.add_argument(
            "--config", type=absolute, default=argparse.SUPPRESS, help="folder configuration file"
        )
        cmd.add_argument(
            "--device", choices=["cpu", "gpu"], default=argparse.SUPPRESS, help="inference device"
        )
        if output:
            cmd.add_argument("--json", action="store_true", help="machine-readable output")
        return cmd

    def indexing_options(cmd):
        cmd.add_argument(
            "folders", type=absolute, nargs="*", help="folders to index; omit to use saved folders"
        )
        cmd.add_argument("--code", action="store_true", help="index code separately; honor .gitignore")
        cmd.add_argument(
            "--keyword-only", action="store_true", help="index names and extracted text without a model"
        )
        cmd.add_argument(
            "--exclude", type=absolute, action="append", default=[], help="skip a folder or file"
        )
        cmd.add_argument(
            "--exclude-ext", action="append", default=[], help="skip extensions, comma-separated"
        )
        cmd.add_argument("--ocr-language", default="eng", help="Tesseract language (default: eng)")
        cmd.add_argument("--max-segments", type=positive, default=5000, help="maximum segments per file")
        cmd.add_argument(
            "--video-interval", type=positive_float, default=5, help="seconds between video frames"
        )
        cmd.add_argument("--reindex", action="store_true", help="re-extract unchanged files")
        cmd.add_argument(
            "--ignore-pattern", action="append", default=[], help="skip a root-relative glob (repeatable)"
        )
        cmd.add_argument("--verbose", action="store_true", help="show detailed indexing progress on stderr")

    indexing_options(command("index", "Index folders; update changed files and remove deleted files"))
    find = command("search", "Search the index")
    find.add_argument("query", nargs="+", help="natural-language query, exact words or code: query")
    find.add_argument("--code", action="store_true", help="search the separate code collection")
    find.add_argument("--keyword-only", action="store_true", help="use only names and extracted text")
    find.add_argument(
        "--kind", choices=["doc", "pdf", "image", "audio", "video", "code"], action="append", default=[]
    )
    find.add_argument("-n", "--limit", type=positive, default=10)
    find.add_argument("--paths", action="store_true", help="print only matching paths, one per line")
    find.add_argument("-0", "--null", action="store_true", help="NUL-delimit paths (implies --paths)")
    command("status", "Show index counts, folders and extraction failures")
    command("config", "Show saved folders and options without opening the database")
    command("doctor", "Check model, worker and extraction tools")
    setup = command("setup", "Build local inference worker and download verified model files", output=False)
    setup.add_argument("--backend", choices=["cpu", "cuda", "vulkan"], default="cpu")
    setup.add_argument("--jobs", type=positive, default=4)
    runtime = command("runtime", "Build the pinned local inference worker", output=False)
    runtime.add_argument("--backend", choices=["cpu", "cuda", "vulkan"], default="cpu")
    runtime.add_argument("--jobs", type=positive, default=4)
    models = command("models", "Download and checksum-verify pinned model files", output=False)
    models.add_argument("action", choices=["download"])
    watch = command("watch", "Poll saved folders for changes until Ctrl-C")
    watch.add_argument("--interval", type=positive_float, default=10, help="seconds between passes")
    watch.add_argument("--once", action="store_true", help="perform one update pass and exit")
    return app


def emit(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False))


def report_index(report, as_json):
    if as_json:
        emit(report, True)
    else:
        print(
            f"{report['indexed']} indexed, {report['unchanged']} unchanged, {report['removed']} removed, "
            f"{report['skipped']} skipped, {len(report['errors'])} errors"
        )
        for error in report["errors"]:
            print(f"  {error}", file=sys.stderr)


def doctor():
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        fts5 = True
    except sqlite3.OperationalError:
        fts5 = False
    finally:
        db.close()
    return {
        "python": sys.version.split()[0],
        "sqlite_fts5": fts5,
        "worker": {"path": str(worker_path()), "installed": worker_path().is_file()},
        "gpu_worker": {"path": str(worker_path("gpu")), "installed": worker_path("gpu").is_file()},
        "models": {
            name: {
                "path": str(model_dir() / name),
                "present": (model_dir() / name).is_file(),
                "size_ok": (model_dir() / name).is_file() and (model_dir() / name).stat().st_size == size,
            }
            for name, (size, _) in MODEL_FILES.items()
        },
        "tools": {
            name: shutil.which(name)
            for name in ("git", "cmake", "c++", "pdftotext", "pdftoppm", "tesseract", "ffmpeg", "ffprobe")
        },
    }


def index_saved(store, roots, reindex=False, verbose=False):
    report = dict(indexed=0, unchanged=0, removed=0, skipped=0, errors=[])
    for root in roots:
        options = root["options"]
        try:
            result = index(
                store,
                [Path(root["path"])],
                code=root["collection"] == "code",
                reindex=reindex,
                verbose=verbose,
                **(
                    options
                    | {
                        "excludes": [Path(p) for p in options["excludes"]],
                        "exclude_ext": set(options["exclude_ext"]),
                    }
                ),
            )
        except DigupError as exc:
            result = dict(indexed=0, unchanged=0, removed=0, skipped=0, errors=[str(exc)])
        for key in ("indexed", "unchanged", "removed", "skipped"):
            report[key] += result[key]
        report["errors"].extend(result["errors"])
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    with device_mode(args.device):
        return run_command(args)


def run_command(args):
    store = None
    try:
        if args.command in {"setup", "runtime"}:
            build_runtime(args.backend, args.jobs)
        if args.command in {"setup", "models"}:
            download_models()
        if args.command in {"setup", "runtime", "models"}:
            return 0
        if args.command == "doctor":
            result = doctor()
            if args.json:
                emit(result, True)
            else:
                print(
                    f"Python {result['python']}; SQLite FTS5: {'OK' if result['sqlite_fts5'] else 'MISSING'}"
                )
                print(
                    f"Worker: {result['worker']['path']} ({'installed' if result['worker']['installed'] else 'run digup setup'})"
                )
                print(
                    f"GPU worker: {result['gpu_worker']['path']} "
                    f"({'installed' if result['gpu_worker']['installed'] else 'build cuda or vulkan backend'})"
                )
                for name, info in result["models"].items():
                    print(
                        f"Model {name}: {'present (size OK)' if info['size_ok'] else 'run digup models download'}"
                    )
                for name, path in result["tools"].items():
                    print(f"{name}: {path or 'missing'}")
            return 0 if result["sqlite_fts5"] else 1
        settings = Settings(args.config)
        if args.command == "config":
            result = {
                "config": str(settings.path),
                "index": str(args.index),
                "folders": settings.folders(args.index),
            }
            if args.json:
                emit(result, True)
            else:
                print(f"Config: {result['config']}")
                print(f"Index: {result['index']}")
                for root in result["folders"]:
                    print(f"  Folder ({root['collection']}): {root['path']}")
                    print(f"    Options: {json.dumps(root['options'], ensure_ascii=False)}")
                if not result["folders"]:
                    print("No saved folders for this index.")
            return 0
        if args.command == "index":
            extensions = {
                "." + ext.lower().lstrip(".")
                for values in args.exclude_ext
                for ext in values.split(",")
                if ext
            }
            if args.folders:
                for folder in args.folders:
                    if folder.is_symlink() or not folder.is_dir():
                        raise DigupError(f"Index root must be an existing directory, not a symlink: {folder}")
                roots = settings.remember(
                    args.index,
                    args.folders,
                    args.code,
                    dict(
                        keyword_only=args.keyword_only,
                        excludes=[str(p) for p in args.exclude],
                        exclude_ext=sorted(extensions),
                        ignore_patterns=args.ignore_pattern,
                        language=args.ocr_language,
                        max_segments=args.max_segments,
                        video_interval=args.video_interval,
                    ),
                )
            else:
                if (
                    args.code
                    or args.keyword_only
                    or args.exclude
                    or extensions
                    or args.ignore_pattern
                    or args.ocr_language != "eng"
                    or args.max_segments != 5000
                    or args.video_interval != 5
                ):
                    raise DigupError(
                        "Supply explicit folders when changing indexing options; "
                        "`digup index` uses each saved folder's options."
                    )
                roots = settings.remember(args.index)
                if not roots:
                    raise DigupError("No saved folders for this index. Run `digup index <folder>` first.")
            # Folder choices are durable even if DB creation, model loading or extraction later fails.
            store = Store(args.index)
            if args.folders:
                report = index(
                    store,
                    args.folders,
                    args.code,
                    args.keyword_only,
                    args.exclude,
                    extensions,
                    args.ocr_language,
                    args.max_segments,
                    args.video_interval,
                    args.reindex,
                    verbose=args.verbose,
                    ignore_patterns=args.ignore_pattern,
                )
            else:
                report = index_saved(store, roots, args.reindex, verbose=args.verbose)
            report_index(report, args.json)
            return 1 if report["errors"] else 0
        if args.command == "watch":
            roots = settings.remember(args.index)
            if not roots:
                raise DigupError("No saved folders for this index. Run `digup index <folder>` first.")
        store = Store(args.index, readonly=args.command in {"search", "status"})
        if args.command == "search":
            results = search(store, " ".join(args.query), args.limit, args.kind, args.code, args.keyword_only)
            if args.json:
                emit(results, True)
            elif args.paths or args.null:
                for result in results:
                    sys.stdout.write(result["path"] + ("\0" if args.null else "\n"))
            else:
                for number, result in enumerate(results, 1):
                    print(f"{number}. {result['path']}  [{result['location']}]")
                    if result["snippet"]:
                        print(f"   {result['snippet']}")
                if not results:
                    print("No matches.")
            return 0
        if args.command == "status":
            result = store.status()
            result.update(config=str(settings.path), roots=settings.folders(args.index))
            if args.json:
                emit(result, True)
            else:
                print(f"Index: {result['index']}")
                print(f"Config: {result['config']}")
                print(
                    f"{result['files']} files · {result['segments']} segments · {result['vectors']} vectors"
                )
                for kind, count in result["kinds"].items():
                    print(f"  {kind}: {count}")
                for root in result["roots"]:
                    print(f"  Folder ({root['collection']}): {root['path']}")
                for failure in result["failures"]:
                    print(f"  Failed: {failure['path']}: {failure['error']}")
            return 0
        if args.command == "watch":
            if not args.json:
                print(
                    f"Watching {len(roots)} saved folder(s), polling every {args.interval:g}s. Ctrl-C to stop."
                )
            while True:
                roots = settings.folders(args.index)
                if not roots:
                    raise DigupError("No saved folders for this index. Run `digup index <folder>` first.")
                report = index_saved(store, roots)
                if args.once or report["indexed"] or report["removed"] or report["errors"]:
                    report_index(report, args.json)
                if args.once:
                    return 1 if report["errors"] else 0
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except (DigupError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        message = str(exc)
        if isinstance(exc, sqlite3.Error) and getattr(exc, "sqlite_errorcode", 0) & 0xFF in (
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB,
        ):
            message += (
                f". Preserve and move aside the damaged index directory {args.index}, then run "
                "`digup index` with the same --index and --config to rebuild from saved folders. "
                f"The separate config is at {args.config}."
            )
        if getattr(args, "json", False):
            print(json.dumps({"error": message}))
        print(f"digup: {message}", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
    return 0
