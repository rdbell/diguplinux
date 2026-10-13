import QtQuick
import QtTest
import "../core/Digup.js" as Digup

TestCase {
  name: "DigUp"
  function context(query, scope, settings) {
    return { query: query, scope: scope || "", settings: settings || {} }
  }
  function test_scope_and_commands() {
    compare(Digup.request(context("digup pets"), Digup.KEY).query, "pets")
    compare(Digup.request(context("pets", Digup.KEY), Digup.KEY).query, "pets")
    compare(Digup.request(context("pets"), Digup.KEY), null)
    compare(Digup.request(context("digup pets", "timer"), Digup.KEY), null)
    compare(Digup.request({command: {rest: "code: retry"}, settings: {}}, Digup.KEY).query, "code: retry")
  }
  function test_settings_and_argv() {
    var a = Digup.request(context("digup --strange"), Digup.KEY)
    var b = Digup.request(context("digup --strange", "", {device: "gpu", keywordOnly: true}), Digup.KEY)
    verify(Digup.cacheKey(a) !== Digup.cacheKey(b))
    var args = Digup.argv("/helper", b)
    verify(args.indexOf("--query=--strange") >= 0)
    verify(args.indexOf("--keyword-only") >= 0)
  }
  function test_actions_and_results() {
    var path = "/tmp/a 'quoted' $(echo nope).txt"
    var rows = Digup.rows({results: [{path: path, snippet: "invoice", location: "page 3"}]})
    compare(rows[0].action.argv, ["xdg-open", path])
    compare(rows[0].altAction.text, path)
    verify(rows[0].subtitle.indexOf("page 3") >= 0)
    compare(rows[0].remember, false)
    compare(Digup.rows({results: [{path: "relative"} ]})[0].title, "No matching files")
    verify(Digup.rows(Digup.parse("bad JSON"))[0].disabled)
    compare(Digup.rows({results: [], error: "Offline"})[0].title, "Offline")
  }
}
