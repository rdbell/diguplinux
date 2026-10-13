# CPU and GPU indexing benchmarks

The inputs in `sources.json` correspond to the supplied Constitution PDF,
Financial Times OCR text and PDF, podcast MP3, Looney Tunes MP4 and three
original PNGs from the Nirvana covers archive. Thumbnail and metadata
derivatives are excluded. Downloads are checked against the recorded sizes
and MD5s, and stored under `~/.cache/diguplinux/benchmarks/inputs/`.

Run from the checkout with the development environment activated:

```bash
python benchmarks/fetch.py
python benchmarks/run.py --device both --case constitution
```

Each selected file runs first on CPU, then on GPU. Files are ordered by encoded
size, and each run uses a fresh SQLite index and a fresh worker. CPU uses eight
threads; GPU uses Vulkan on the Apple M2 Max with float16 compute disabled.
The GPU worker confirms hardware offload, excluding software Vulkan devices.
No user configuration or personal index is modified.

Timings include worker startup, embeddings, extraction, storage and cleanup.
Builds, downloads and correctness checks are excluded. Extraction includes
PDF rendering, OCR and FFmpeg decoding, all on CPU. OS and driver caches are
warm; results are individual runs on a desktop machine, not repeated averages.

Full CPU indexing of the newspaper text was stopped after its throughput
projected a 15–20 minute run. To limit CPU time, subsequent slow-file comparisons
use identical samples on both devices:

```bash
python benchmarks/run.py --device both --case financial-times-text --segment-limit 16
python benchmarks/run.py --device both --case financial-times-pdf --segment-limit 16
python benchmarks/run.py --device both --case cover-1 --case cover-3 --case cover-2
python benchmarks/run.py --device both --case podcast --media-seconds 60
python benchmarks/run.py --device both --case video --media-seconds 60
```

`--segment-limit` keeps the first N segments from the original reader. For the
newspaper PDF, the first 16 segments contain one page image and 15 text chunks.
`--media-seconds 60` limits window/frame **start times**, reading the original
media file. Audio uses three full 30-second windows starting at 0, 20 and 40
seconds, covering 0–70 seconds. Video adds 12 frames at 0, 5, …, 55 seconds.
The samples do not represent full-file indexing times or retrieval-quality
evaluations. Normal CLI indexing still processes complete files.

Each run writes its indexes, worker logs and `results.json` under the benchmark
cache, or a new directory selected with `--output`. Result records identify
full versus sampled scope, segment counts, stage timings and the confirmed
worker backend. Interrupted runs under competing CPU load are excluded from
the report.

To combine completed paired runs:

```bash
python benchmarks/report.py /path/to/run1 /path/to/run2 --output benchmarks/results.json
```

The report verifies identical extracted segments and compares every stored
CPU/GPU vector using cosine similarity. It refuses comparisons with different
segment counts or scopes. Cached source files and SQLite indexes are not
included in this repository; only the benchmark code and numerical results are.
