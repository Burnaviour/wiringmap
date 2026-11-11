const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");
const { join } = require("node:path");

// Load the offline viewer without a browser. Replace only its final mount line
// to expose buildView; the production script does not acquire test-only APIs.
const source = readFileSync(join(__dirname, "../src/wiringmap/render/static/app.js"), "utf8");
const anchor = '  const mount = document.getElementById("app");';
assert.ok(source.includes(anchor));

const N = (id, kind, name = id) => ({ id, kind, name, attrs: {} });
const C = (source, target) => ({ kind: "contains", source, target });
const I = (source, target) => ({ kind: "imports", source, target });
const F = (source, target) => ({ kind: "calls", source, target, attrs: { confidence: "resolved" } });
const graph = {
  root_name: "test", nodes: [
    N("dir:.", "directory"), N("dir:pkg", "directory"), N("dir:pkg/core", "directory"),
    N("pkg.a", "module", "a.py"), N("pkg.b", "module", "b.py"),
    N("pkg.core.shared", "module", "shared.py"), N("pkg.core.helper", "module", "helper.py"),
    N("ext:tool", "external", "tool"),
    N("pkg.a:f", "function"), N("pkg.b:f", "function"),
    N("pkg.core.shared:f", "function"), N("pkg.core.helper:f", "function"),
  ],
  edges: [
    C("dir:.", "dir:pkg"), C("dir:pkg", "dir:pkg/core"),
    C("dir:pkg", "pkg.a"), C("dir:pkg", "pkg.b"),
    C("dir:pkg/core", "pkg.core.shared"), C("dir:pkg/core", "pkg.core.helper"),
    C("pkg.a", "pkg.a:f"), C("pkg.b", "pkg.b:f"),
    C("pkg.core.shared", "pkg.core.shared:f"), C("pkg.core.helper", "pkg.core.helper:f"),
    I("pkg.a", "pkg.core.shared"), I("pkg.a", "pkg.core.helper"),
    I("pkg.b", "pkg.core.shared"), I("pkg.b", "pkg.a"), I("pkg.b", "ext:tool"),
    F("pkg.a:f", "pkg.core.shared:f"), F("pkg.b:f", "pkg.a:f"),
    F("pkg.core.helper:f", "pkg.a:f"),
  ],
};
const window = {};
const document = { getElementById: () => ({ textContent: JSON.stringify({ graph }) }) };
const htmPreact = { html: () => null, render: () => null };
vm.runInNewContext(source.replace(anchor, '  window.buildViewForTest = buildView;\n' + anchor), {
  window, document, htmPreact,
});
const view = (expanded, mode = "imports", muted = [], showExternal = false) =>
  window.buildViewForTest(new Set(expanded), mode, showExternal, new Set(muted));
const edges = (v) => [...v.elements.filter((e) => e.group === "edges")
  .map((e) => `${e.data.source}->${e.data.target}`)].sort();
const allOpen = ["dir:pkg", "dir:pkg/core"];

test("hiding a module removes only its own import arrows", () => {
  assert.deepEqual(edges(view(allOpen, "imports", ["pkg.core.shared"])), [
    "pkg.a->pkg.core.helper", "pkg.b->pkg.a",
  ]);
  assert.deepEqual(edges(view(allOpen)), [
    "pkg.a->pkg.core.helper", "pkg.a->pkg.core.shared",
    "pkg.b->pkg.a", "pkg.b->pkg.core.shared",
  ]);
});
test("collapsed folder keeps arrows from unsuppressed children", () => {
  const v = view(["dir:pkg"], "imports", ["pkg.core.shared"]);
  const arrows = edges(v);
  assert.ok(arrows.includes("pkg.a->dir:pkg/core"));
  assert.ok(arrows.includes("pkg.b->pkg.a"));
  assert.equal(arrows.length, 2);
  const remaining = v.elements.find((e) => e.data.source === "pkg.a" && e.data.target === "dir:pkg/core");
  assert.equal(remaining.data.n, 1);
});
test("hiding a folder suppresses its descendants, including in calls mode", () => {
  assert.deepEqual(edges(view(allOpen, "imports", ["dir:pkg/core"])), ["pkg.b->pkg.a"]);
  assert.deepEqual(edges(view(allOpen, "calls", ["dir:pkg/core"])), ["pkg.b->pkg.a"]);
  assert.deepEqual(edges(view(allOpen, "calls", ["pkg.core.shared"])), [
    "pkg.b->pkg.a", "pkg.core.helper->pkg.a",
  ]);
});
test("muting external package preserves its node when packages are shown", () => {
  const v = view(allOpen, "imports", ["ext:tool"], true);
  assert.ok(v.nodeIds.has("ext:tool"));
  assert.ok(!edges(v).includes("pkg.b->ext:tool"));
  assert.ok(edges(view(allOpen, "imports", [], true)).includes("pkg.b->ext:tool"));
});
