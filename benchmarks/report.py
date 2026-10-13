"""Summarize paired runs and compare every stored CPU/GPU vector for each workload."""

import argparse
import json
import math
import sqlite3
from pathlib import Path

from diguplinux.store import unpack


def parity(folder, case):
    rows = []
    for device in ["cpu", "gpu"]:
        database = folder / device / case / "index.sqlite"
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            rows.append(
                connection.execute(
                    "SELECT path,text,location,line,page,seconds,vector FROM segments ORDER BY id"
                ).fetchall()
            )
    if len(rows[0]) != len(rows[1]):
        raise ValueError(f"Different segment counts: {case}")
    scores = []
    for cpu, gpu in zip(*rows):
        if cpu[:-1] != gpu[:-1]:
            raise ValueError(f"Different extracted segments: {case}")
        if cpu[-1] is None or gpu[-1] is None:
            if cpu[-1] != gpu[-1]:
                raise ValueError(f"Different missing embeddings: {case}")
            continue
        a, b = unpack(cpu[-1]), unpack(gpu[-1])
        if len(a) != len(b):
            raise ValueError(f"Different vector dimensions: {case}")
        scores.append(
            sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
        )
    return {
        "vectors_compared": len(scores),
        "cosine_min": min(scores),
        "cosine_mean": sum(scores) / len(scores),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results.json"))
    args = parser.parse_args()
    pairs = []
    for folder in args.runs:
        results = json.loads((folder / "results.json").read_text())
        cases = dict.fromkeys(row["case"] for row in results)
        for case in cases:
            modes = {row["device"]: row for row in results if row["case"] == case}
            cpu, gpu = modes["cpu"], modes["gpu"]
            if cpu["segments"] != gpu["segments"] or cpu.get("scope", "full") != gpu.get("scope", "full"):
                raise ValueError(f"Different workload scope: {case}")
            pairs.append(
                {
                    "case": case,
                    "input_bytes": cpu["input_bytes"],
                    "scope": cpu.get("scope", "full"),
                    "cpu": cpu,
                    "gpu": gpu,
                    "speedup": cpu["wall_seconds"] / gpu["wall_seconds"],
                    "parity": parity(folder, case),
                }
            )
    pairs.sort(key=lambda pair: pair["input_bytes"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(pairs, indent=2) + "\n")
    print("| Case | Scope | Segments | CPU seconds | GPU seconds | Speedup | Min cosine |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for pair in pairs:
        print(
            f"| {pair['case']} | {pair['scope']} | {pair['cpu']['segments']} | "
            f"{pair['cpu']['wall_seconds']:.2f} | {pair['gpu']['wall_seconds']:.2f} | "
            f"{pair['speedup']:.2f}× | {pair['parity']['cosine_min']:.6f} |"
        )


if __name__ == "__main__":
    main()
