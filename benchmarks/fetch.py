"""Download the pinned benchmark inputs into DigUp's cache, never into the repository."""

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from diguplinux.config import cache_dir


def md5(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "md5").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=cache_dir() / "benchmarks")
    args = parser.parse_args()
    sources = json.loads(Path(__file__).with_name("sources.json").read_text())
    manifest = []
    for source in sources:
        folder = args.output / "inputs" / source["id"]
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / source["filename"]
        if not target.is_file() or target.stat().st_size != source["bytes"] or md5(target) != source["md5"]:
            temporary = target.with_suffix(target.suffix + ".part")
            try:
                request = urllib.request.Request(source["url"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                if temporary.stat().st_size != source["bytes"] or md5(temporary) != source["md5"]:
                    raise ValueError(f"Benchmark input changed or download failed: {source['id']}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        manifest.append(source | {"path": str(target.resolve())})
        print(f"Verified {source['id']}: {source['bytes']} bytes", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
