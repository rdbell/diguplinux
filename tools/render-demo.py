#!/usr/bin/env python3
"""Render real Keystroke offscreen, searching a temporary synthetic DigUp index.

Rendering scaffolding adapted from rdbell/keystroke-hister (MIT).
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = {
    "Invoice INV-2048.txt": "Invoice INV-2048: laptop repair, total $120. Payment received.",
    "Invoice INV-2089.txt": "Invoice INV-2089: office supplies, total $45. Payment due Friday.",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keystroke", type=Path, default=Path.home() / ".config/omarchy/plugins/evindor.keystroke"
    )
    parser.add_argument("--digup", type=Path, default=Path.home() / ".local/bin/digup")
    parser.add_argument("--device", choices=["cpu", "gpu"], default="cpu")
    parser.add_argument("--output", type=Path, default=ROOT / "assets/demo.gif")
    args = parser.parse_args()
    executable = args.digup.absolute()
    if not (args.keystroke / "Keystroke.qml").is_file():
        raise SystemExit("Pass --keystroke with a Keystroke source directory")
    with tempfile.TemporaryDirectory(prefix="du-") as temp:
        work = Path(temp)
        project = work / "project"
        shutil.copytree(
            args.keystroke,
            project,
            ignore=shutil.ignore_patterns(
                ".git", ".claude", ".agents", ".codex", "tests", "__pycache__", "experiments"
            ),
        )
        (work / "qs").symlink_to("/usr/share/omarchy/shell")
        source = project / "Keystroke.qml"
        qml = source.read_text().replace(
            "  PanelWindow {", "  Window {\n    transientParent: null\n    width: 1000; height: 800"
        )
        qml = qml.replace("    anchors { top: true; bottom: true; left: true; right: true }\n", "")
        qml = qml.replace(
            "  function inspect() {", "  readonly property var showcaseCard: card\n  function inspect() {", 1
        )
        source.write_text(
            "\n".join(
                line
                for line in qml.splitlines()
                if "exclusionMode:" not in line and "WlrLayershell." not in line
            )
        )
        local = work / ".local/share/keystroke/extensions/keystroke-digup"
        local.parent.mkdir(parents=True)
        local.symlink_to(ROOT / "extensions/keystroke-digup")
        frames = work / "frames"
        frames.mkdir()
        documents = work / "docs"
        documents.mkdir()
        for name, text in DOCUMENTS.items():
            (documents / name).write_text(text)
        spec = importlib.util.spec_from_file_location("fixtures", ROOT / "tests/fixtures.py")
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        fixtures.pdf(
            documents / "Lease-2026.pdf",
            [
                "Building maintenance schedule.",
                "Tenants may keep domestic animals in the apartment with written approval.",
            ],
        )
        fixtures.landscape(work / "scene.ppm")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:duration=4",
                str(documents / "Recording-01.wav"),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-i",
                str(work / "scene.ppm"),
                "-t",
                "6",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(documents / "Clip-01.mp4"),
            ],
            check=True,
        )
        index = work / "index"
        subprocess.run(
            [
                str(executable),
                "index",
                str(documents),
                "--device",
                args.device,
                "--index",
                str(index),
                "--config",
                str(work / "digup-config.toml"),
            ],
            check=True,
            capture_output=True,
        )
        examples = [
            ("can I keep a dog", "Lease-2026.pdf"),
            ("paid for computer repairs", "Invoice INV-2048.txt"),
            ("steady electronic beep", "Recording-01.wav"),
            ("green hills under blue sky", "Clip-01.mp4"),
        ]
        for query, expected in examples:
            result = subprocess.run(
                [str(executable), "search", query, "--device", args.device, "--index", str(index), "--json"],
                capture_output=True,
                text=True,
                check=True,
            )
            rows = json.loads(result.stdout)
            if not rows or Path(rows[0]["path"]).name != expected:
                raise SystemExit(f"Unexpected search ranking for {query}: {result.stdout}")
            print(f"Verified: {query} -> {expected} [{rows[0]['location']}]", flush=True)
        scenes = []
        for query, expected in examples:
            full = "digup " + query
            for count in range(len("digup "), len(full) + 1):
                scenes.append({"query": full[:count], "wait": 60, "copies": 1})
            scenes.append({"query": full, "wait": 300, "copies": 25, "expected": expected})
            scenes.append({"ctrl": True, "wait": 150, "copies": 8})
            scenes.append({"ctrl": False, "wait": 100, "copies": 4})
        config = {
            "version": 1,
            "palette": {"accent": "sky", "density": "compact", "animations": "off"},
            "matching": {"mode": "off"},
            "providers": {
                "keystroke-digup": {
                    "enabled": True,
                    "executable": str(executable),
                    "indexPath": str(index),
                    "keywordOnly": False,
                    "limit": 1,
                    "device": args.device,
                }
            },
        }
        harness = r"""
    import QtQuick
    import Quickshell
    import "project"
    ShellRoot {
      id: test
      property int stage: 0
      property int index: 0
      property int frame: 0
      property bool busy: false
      property double deadline: 0
      property var scenes: SCENES
      Keystroke { id: palette; omarchyPath: "/usr/share/omarchy" }
      Timer { interval: 30; running: true; repeat: true; onTriggered: {
if (test.busy || Date.now() < test.deadline) return
if (test.stage === 0) {
  if (!palette.registry.manifests["keystroke-digup"]) return
  palette.applyConfigText(JSON.stringify(CONFIG)); test.stage = 1; return
}
if (test.stage === 1) {
  var entry = palette.registry.entries.filter(function(e) { return e.key === "keystroke-digup" })[0]
  if (!entry || !entry.loaded) return
  palette.open(JSON.stringify({query: "digup "})); test.stage = 2; test.deadline = Date.now() + 300; return
}
var scene = test.scenes[test.index]
if (!scene) { console.log("PASS demo"); Qt.quit(); return }
if (test.stage === 2) {
  if (scene.query !== undefined) { palette.ctrlHeld = false; palette.setQuery(scene.query) }
  if (scene.select !== undefined) palette.selected = scene.select
  if (scene.ctrl !== undefined) palette.ctrlHeld = scene.ctrl
  test.deadline = Date.now() + scene.wait; test.stage = 3; return
}
if (scene.expected && !palette.rows.some(function(row) { return row.title === scene.expected && !row.disabled })) return
if (scene.expected) {
  var index = palette.rows.findIndex(function(row) { return row.title === scene.expected })
  palette.selected = index
}
test.busy = true
palette.showcaseCard.grabToImage(function(result) {
  for (var i = 0; i < scene.copies; i++) {
    var name = String(test.frame++).padStart(4, "0")
    result.saveToFile(FRAMES + "/" + name + ".png")
  }
  test.busy = false; test.index++; test.stage = 2
})
      } }
      Timer { interval: 120000; running: true; onTriggered: {console.log("FAIL demo timeout"); Qt.quit()} }
    }
    """
        harness = harness.replace("SCENES", json.dumps(scenes)).replace("CONFIG", json.dumps(config))
        harness = harness.replace("FRAMES", json.dumps(str(frames)))
        (work / "shell.qml").write_text(harness)
        env = dict(
            os.environ,
            HOME=str(work),
            XDG_CONFIG_HOME=str(work / ".config"),
            XDG_DATA_HOME=str(work / ".local/share"),
            DIGUP_MODELS=os.environ.get("DIGUP_MODELS", str(Path.home() / ".local/share/diguplinux/models")),
            DIGUP_WORKER=str(
                Path.home()
                / ".local/share/diguplinux/bin"
                / ("digup-worker-vulkan" if args.device == "gpu" else "digup-worker")
            ),
            XDG_STATE_HOME=str(work / ".local/state"),
            XDG_RUNTIME_DIR=str(work),
            QT_QPA_PLATFORM="offscreen",
            QT_QPA_PLATFORMTHEME="generic",
            QT_QUICK_BACKEND="software",
            QML_IMPORT_PATH=str(work),
        )
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        result = subprocess.run(
            ["quickshell", "-p", str(work / "shell.qml")],
            env=env,
            capture_output=True,
            text=True,
            timeout=130,
        )
        output = result.stdout + result.stderr
        if "PASS demo" not in output or "FAIL" in output:
            raise SystemExit(output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        wallpaper = work / "wallpaper.png"
        subprocess.run(
            ["rsvg-convert", "-o", str(wallpaper), str(ROOT / "assets/demo-wallpaper.svg")], check=True
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                "10",
                "-loop",
                "1",
                "-i",
                str(wallpaper),
                "-framerate",
                "10",
                "-i",
                str(frames / "%04d.png"),
                "-filter_complex",
                "[0:v][1:v]overlay=(W-w)/2:(H-h)/2:shortest=1,split[a][b];"
                "[a]palettegen=max_colors=256:reserve_transparent=0[p];"
                "[b][p]paletteuse=dither=bayer:bayer_scale=4",
                "-loop",
                "0",
                str(args.output),
            ],
            check=True,
        )
        # One representative still for the author's visual review, outside the repo.
        shutil.copy2(frames / "0030.png", "/tmp/keystroke-digup-demo-review.png")
        print(f"Rendered {len(list(frames.glob('*.png')))} frames to {args.output}")


if __name__ == "__main__":
    main()
