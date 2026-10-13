pragma ComponentBehavior: Bound
import QtQuick
import Quickshell.Io
import "core/Digup.js" as Digup

QtObject {
  id: root
  property var shell: null
  property var extension: null
  property string omarchyPath: ""
  property var host: null
  readonly property string key: extension && extension.id ? String(extension.id) : Digup.KEY
  readonly property string helper: decodeURIComponent(String(Qt.resolvedUrl("bin/search.py")).replace(/^file:\/\//, ""))
  property var subject: null
  property var active: null
  property var cache: ({})

  readonly property var provider: ({
    apiVersion: 1, name: "DigUp", icon: Digup.ICON, color: "#61afef",
    description: "Search the full text of your indexed files",
    settings: Digup.SETTINGS,
    query: function(ctx) { return root.query(ctx) },
    opened: function() { root.reset() },
    dismiss: function() { root.reset() }
  })

  function cancel() {
    if (active) {
      active.cancelled = true
      if (active.running) active.signal(15)
      active = null
    }
  }
  function reset() { debounce.stop(); cancel(); subject = null; cache = ({}) }
  function query(ctx) {
    host = ctx.host || host
    var req = Digup.request(ctx, key)
    if (!req || !req.query) {
      debounce.stop(); cancel(); subject = null
      if (req) return [Digup.status("Search your DigUp index", "Describe a file, or enter words from its contents")]
      if (ctx.scope) return []
      return [{ id: "digup-open", title: "Search DigUp", subtitle: "Search local files by meaning and exact words",
                icon: Digup.ICON, keywords: "files documents images audio video code", section: "DigUp",
                action: { type: "navigate", scope: key, title: "DigUp" }, verb: "Search" }]
    }
    var k = Digup.cacheKey(req)
    if (!subject || Digup.cacheKey(subject) !== k) {
      cancel(); subject = req; debounce.restart()
    }
    var hit = cache[k]
    if (hit !== undefined) return Digup.rows(hit)
    if (ctx.pending) ctx.pending()
    return [Digup.status("Searching DigUp…", "")]
  }

  readonly property Timer debounce: Timer {
    interval: 250
    onTriggered: root.search()
  }
  function search() {
    if (!subject) return
    var k = Digup.cacheKey(subject)
    if (cache[k] !== undefined || (active && active.requestKey === k)) return
    var proc = fetcher.createObject(root, { requestKey: k })
    active = proc
    proc.command = Digup.argv(helper, subject)
    proc.running = true
  }
  readonly property Component fetcher: Component {
    Process {
      id: proc
      property string requestKey: ""
      property bool cancelled: false
      property bool outputDone: false
      property bool exitDone: false
      property int code: 0
      property string output: ""
      property bool timedOut: false
      property bool finished: false
      readonly property Timer watchdog: Timer {
        interval: 30000
        onTriggered: { proc.timedOut = true; if (proc.running) proc.signal(15) }
      }
      onRunningChanged: { if (running) watchdog.start(); else watchdog.stop() }
      stdout: StdioCollector {
        onStreamFinished: { proc.output = text; proc.outputDone = true; root.finish(proc) }
      }
      onExited: function(exitCode) { proc.code = exitCode; proc.exitDone = true; root.finish(proc) }
    }
  }
  function finish(proc) {
    // Process exit and stdout completion may arrive in either order.
    if (!proc.outputDone || !proc.exitDone || proc.finished) return
    proc.finished = true
    var refresh = false
    if (!proc.cancelled && active === proc && subject && Digup.cacheKey(subject) === proc.requestKey) {
      var value = proc.timedOut ? { results: [], error: "DigUp search timed out" }
        : proc.code === 0 ? Digup.parse(proc.output)
        : { results: [], error: "Search helper failed; check that Python 3 is installed" }
      var next = ({})
      var keys = Object.keys(cache).slice(-15)
      for (var i = 0; i < keys.length; i++) next[keys[i]] = cache[keys[i]]
      next[proc.requestKey] = value
      cache = next
      active = null
      refresh = true
    }
    proc.destroy()
    if (refresh && host) host.requery({ catalog: false, provider: key })
  }
  Component.onDestruction: root.cancel()
}
