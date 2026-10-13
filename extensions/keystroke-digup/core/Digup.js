.pragma library

var KEY = "keystroke-digup"
var ICON = "󰈞"
var SETTINGS = [
  { key: "executable", type: "string", label: "DigUp executable", default: "digup",
    description: "Executable name on PATH or an absolute path." },
  { key: "indexPath", type: "string", label: "Index directory", default: "",
    description: "Leave empty to use DigUp's default index." },
  { key: "device", type: "string", label: "Inference device", default: "cpu",
    description: "cpu or gpu. GPU requires an installed Vulkan or CUDA worker." },
  { key: "keywordOnly", type: "boolean", label: "Keyword-only search", default: false,
    description: "Skip model loading and search extracted text and names only." },
  { key: "limit", type: "number", label: "Maximum results", default: 12, min: 1, max: 50, integer: true }
]

function request(ctx, key) {
  var scoped = ctx.scope === key
  if (ctx.scope && !scoped) return null
  var settings = ctx.settings || {}
  var text = String(ctx.query || "").trim()
  if (ctx.command) text = String(ctx.command.rest || "").trim()
  else if (!scoped) {
    var prefix = String(settings.prefix || "digup")
    if (text.toLowerCase() === prefix.toLowerCase()) text = ""
    else if (text.toLowerCase().indexOf(prefix.toLowerCase() + " ") === 0) text = text.slice(prefix.length).trim()
    else return null
  }
  return { query: text, executable: String(settings.executable || "digup").trim(),
           indexPath: String(settings.indexPath || "").trim(), device: String(settings.device || "cpu"),
           keywordOnly: settings.keywordOnly === true,
           limit: Math.max(1, Math.min(50, Math.round(Number(settings.limit) || 12))) }
}
function cacheKey(req) { return JSON.stringify(req) }
function argv(helper, req) {
  var args = ["python3", helper, "--executable", req.executable, "--index-path", req.indexPath,
              "--device", req.device, "--limit", String(req.limit), "--query=" + req.query]
  if (req.keywordOnly) args.push("--keyword-only")
  return args
}
function status(title, subtitle) {
  return { id: "digup-status", title: title, subtitle: subtitle || "", icon: ICON,
           section: "DigUp", score: 100, disabled: true, remember: false }
}
function parse(text) {
  try {
    var value = JSON.parse(text)
    if (value && Array.isArray(value.results)) return value
  } catch (_) {}
  return { results: [], error: "DigUp returned an unreadable response" }
}
function safePath(path) { return typeof path === "string" && path.charAt(0) === "/" && path.indexOf("\u0000") < 0 }
function rows(result) {
  if (result.error) return [status(result.error, "Check DigUp and this extension's settings")]
  var items = result.results.filter(function(item) { return item && safePath(item.path) })
  if (!items.length) return [status("No matching files", "Try another query, or index more folders with DigUp")]
  return items.map(function(item, i) {
    var detail = item.path + (item.location ? " · " + item.location : "")
    if (item.snippet) detail += " — " + item.snippet
    return { id: "digup:" + item.path, title: item.path.split("/").pop(), subtitle: detail,
             icon: ICON, section: "DigUp", score: 1000 - i, order: i, remember: false,
             verb: "Open", action: { type: "exec", argv: ["xdg-open", item.path] },
             altVerb: "Copy path", altAction: { type: "copy", text: item.path } }
  })
}
