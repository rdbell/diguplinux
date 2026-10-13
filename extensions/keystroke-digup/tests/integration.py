"""Exercise the provider in isolated Quickshell with a temporary DigUp index."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run():
    with tempfile.TemporaryDirectory(prefix="keystroke-digup-") as folder:
        base = Path(folder)
        docs = base / "docs"
        docs.mkdir()
        path = docs / "invoice.txt"
        path.write_text("Invoice nebularabbits payment declined")
        exe = Path.home() / ".local/bin/digup"
        index = base / "index"
        subprocess.run(
            [
                str(exe),
                "index",
                str(docs),
                "--keyword-only",
                "--index",
                str(index),
                "--config",
                str(base / "config.toml"),
            ],
            check=True,
            capture_output=True,
        )
        qml = r"""
import QtQuick
import "EXTENSION" as Extension
QtObject {
  id: test
  property int stage: 0
  property string queryText: "nebularabbits"
  readonly property QtObject service: Extension.Service { extension: ({id: "keystroke-digup"}) }
  function refresh() {
    return service.provider.query({query: "digup " + queryText, scope: "", settings: {
      executable: EXECUTABLE, indexPath: INDEX, keywordOnly: true
    }, host: test, pending: function() {}})
  }
  function fail(message) { console.log("FAIL " + message); Qt.quit() }
  function requery(_) {
    var rows = refresh()
    if (stage === 0) {
      if (rows[0].action.argv[1] !== FILE || rows[0].altAction.text !== FILE) { fail("actions"); return }
      if (rows[0].subtitle.indexOf("nebularabbits") < 0) { fail("snippet"); return }
      stage = 1; queryText = "missingzzzzzz"; refresh()
    } else {
      if (rows[0].title !== "No matching files") { fail("empty search"); return }
      service.provider.dismiss()
      if (service.active || Object.keys(service.cache).length) { fail("reset"); return }
      console.log("PASS integration: search, actions, snippets, empty results, reset")
      Qt.quit()
    }
  }
  readonly property Timer timeout: Timer {interval: 10000; running: true; onTriggered: test.fail("timeout")}
  Component.onCompleted: refresh()
}
"""
        qml = qml.replace("EXTENSION", ROOT.as_uri()).replace("EXECUTABLE", json.dumps(str(exe)))
        qml = qml.replace("INDEX", json.dumps(str(index))).replace("FILE", json.dumps(str(path)))
        harness = base / "shell.qml"
        harness.write_text(qml)
        runtime = base / "runtime"
        runtime.mkdir(mode=0o700)
        env = dict(
            os.environ,
            QT_QPA_PLATFORM="offscreen",
            QT_QPA_PLATFORMTHEME="generic",
            QT_QUICK_BACKEND="software",
            XDG_RUNTIME_DIR=str(runtime),
        )
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        result = subprocess.run(
            ["quickshell", "-p", str(harness)], env=env, cwd=base, capture_output=True, text=True, timeout=15
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0 and "PASS integration:" in output and "FAIL" not in output, output
        assert "TypeError" not in output and "ReferenceError" not in output, output
        print("PASS isolated DigUp + Quickshell integration")


if __name__ == "__main__":
    run()
