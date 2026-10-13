#!/usr/bin/env python3
"""Read-only adapter from DigUp JSON search to Keystroke results."""

import argparse
import json
import os
import signal
import subprocess
from pathlib import Path


def search(executable, query, index_path="", device="cpu", limit=12, keyword_only=False):
    if device not in {"cpu", "gpu"}:
        return {"results": [], "error": "Inference device must be cpu or gpu"}
    if not query.strip():
        return {"results": []}
    if len(query) > 4096:
        return {"results": [], "error": "Search query is too long"}
    limit = max(1, min(50, limit))
    args = [
        str(Path(executable).expanduser()) if "/" in executable else executable,
        "search",
        "--json",
        "--device",
        device,
        "--limit",
        str(limit),
    ]
    if index_path:
        args += ["--index", str(Path(index_path).expanduser())]
    if keyword_only:
        args.append("--keyword-only")
    args += ["--", query]
    # AppImage variables can change system Python's executable detection.
    environment = {k: v for k, v in os.environ.items() if k not in {"APPIMAGE", "APPDIR", "ARGV0"}}
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )

        def stop(signum, frame):
            raise SystemExit(128 + signum)

        previous = signal.signal(signal.SIGTERM, stop)
        try:
            output, _ = process.communicate(timeout=25)
        finally:
            # Also terminate a worker left behind after CLI exit or cancellation.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            signal.signal(signal.SIGTERM, previous)
        payload = json.loads(output)
        if process.returncode:
            message = payload.get("error") if isinstance(payload, dict) else None
            return {"results": [], "error": message or "DigUp search failed"}
        if not isinstance(payload, list):
            raise ValueError("Expected results array")
        results = []
        for item in payload[:limit]:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
                continue
            results.append(
                {
                    "path": path,
                    "location": str(item.get("location") or ""),
                    "snippet": str(item.get("snippet") or ""),
                }
            )
        return {"results": results}
    except FileNotFoundError:
        return {"results": [], "error": "DigUp executable not found; set its path in extension settings"}
    except subprocess.TimeoutExpired:
        return {"results": [], "error": "DigUp search timed out; try GPU or keyword-only mode"}
    except (ValueError, UnicodeError):
        return {"results": [], "error": "DigUp returned an unreadable response"}
    except OSError as exc:
        return {"results": [], "error": f"Cannot run DigUp: {exc}"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="digup")
    parser.add_argument("--index-path", default="")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--keyword-only", action="store_true")
    parser.add_argument("--query", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            search(args.executable, args.query, args.index_path, args.device, args.limit, args.keyword_only),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
