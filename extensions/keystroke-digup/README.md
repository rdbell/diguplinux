# DigUp for Keystroke

Search DigUpLinux's local file index from Omarchy's Keystroke launcher.
Results show the file path, matched page/line/time and extracted snippet.
Enter opens the file with its default application; Ctrl+Enter copies its path.
Opening a file does not jump to the matched page or timestamp.

From the project root, link this extension:

```bash
python3 extensions/keystroke-digup/bin/install
```

Open Keystroke, type `ext`, select **DigUp**, and enable it. Then use
`digup rental agreement` or `digup code: retry`. The code query searches the
separate code collection, which must already have been indexed with `--code`.
This extension only searches; use the CLI to select and index folders.

In the extension's settings, select the DigUp executable (default `digup` on
PATH), optional index directory, CPU/GPU device, keyword-only mode and result
limit. On this machine the executable is `/home/developer/.local/bin/digup`.
If the shell's PATH differs from your terminal, set that absolute path.
GPU mode requires DigUp's Vulkan or CUDA worker. CPU is the portable default.

Queries are debounced for 250 ms. Results are cached while the launcher is
open; close and reopen it to refresh results after indexing. Semantic queries
start a fresh CLI/model worker and scan indexed vectors, so large indexes may
feel slow. Keyword-only mode avoids loading the model. Searches time out after
25 seconds and display a useful error. A persistent search service is a future
optimization. Cancelling a query also stops its CLI and model worker.

The helper passes argument arrays, never shell commands. Queries and paths
stay local. Result paths are not added to Keystroke's usage history
(`remember: false`). Python 3, `digup`, and `xdg-open` are required.

The installer creates only a symlink at
`~/.local/share/keystroke/extensions/keystroke-digup` and refuses to replace an
existing extension. To uninstall, disable it in Keystroke and remove that link.
Keep this checkout in place while linked.

Validation from the project root:

```bash
python3 -m unittest discover -s extensions/keystroke-digup/tests
~/.config/omarchy/plugins/evindor.keystroke/bin/keystroke check-extensions extensions/keystroke-digup
```

Provider lifecycle and installer adapted from rdbell's MIT-licensed
[keystroke-hister](https://github.com/rdbell/keystroke-hister). Its license is
included in this directory.

An isolated integration check runs the QML provider in a separate, offscreen
Quickshell instance against a temporary keyword-only index:

```bash
python3 extensions/keystroke-digup/tests/integration.py
```

It requires the installed DigUp CLI and Quickshell and does not modify your
personal index or running desktop shell.
