const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");
const { join } = require("node:path");

const source = readFileSync(join(__dirname, "../src/wiringmap/render/static/app.js"), "utf8");
const anchor = '  const mount = document.getElementById("app");';
assert.ok(source.includes(anchor));
const graph = { root_name: "test", nodes: [{ id: "dir:.", kind: "directory", name: "root" }], edges: [] };
const window = {};
const document = { getElementById: () => ({ textContent: JSON.stringify({ graph }) }) };
const htmPreact = { html: () => null, render: () => null };
vm.runInNewContext(source.replace(anchor, '  window.layoutTests = { layerRanks, layeredPositions };\n' + anchor), {
  window, document, htmPreact,
});
const { layerRanks, layeredPositions } = window.layoutTests;

test("longest-path ranking keeps shortcuts and transitive calls pointing downward", () => {
  const ranks = layerRanks(["cli", "engine", "types"], [
    ["cli", "engine"], ["engine", "types"], ["cli", "types"],
  ]);
  assert.equal(ranks.get("cli"), 0);
  assert.equal(ranks.get("engine"), 1);
  assert.equal(ranks.get("types"), 2);
});

test("strongly connected callers share a layer but downstream dependencies go below", () => {
  const ranks = layerRanks(["a", "b", "c", "independent"], [
    ["a", "b"], ["b", "a"], ["b", "c"],
  ]);
  assert.equal(ranks.get("a"), ranks.get("b"));
  assert.ok(ranks.get("c") > ranks.get("b"));
  assert.equal(ranks.get("independent"), 0);
});

function node(id, kind = "module", children = []) {
  return {
    id: () => id, children: () => ({ toArray: () => children }),
    isParent: () => children.length > 0,
    data: (key) => ({ kind, label: id, w: 95 })[key],
  };
}
function cy(roots, nodes, links) {
  return {
    nodes: () => ({ orphans: () => ({ toArray: () => roots }) }),
    edges: () => ({ map: (fn) => links.map(([a, b]) => fn({
      source: () => nodes[a], target: () => nodes[b],
    })) }),
  };
}
test("expanded folders keep their children together while routing calls downward", () => {
  const a = node("a"), b = node("b"), c = node("c");
  const folder = node("folder", "group", [a, b]);
  const positions = layeredPositions(cy([folder, c], { a, b, c }, [["a", "b"], ["b", "c"]]));
  assert.ok(positions.get("a").y < positions.get("b").y, "internal edge");
  assert.ok(positions.get("b").y < positions.get("c").y, "external edge");
  assert.equal(positions.size, 3);
});
test("disconnected nodes are placed side by side on the same rank", () => {
  const a = node("a"), b = node("b");
  const positions = layeredPositions(cy([a, b], { a, b }, []));
  assert.equal(positions.get("a").y, positions.get("b").y);
  assert.notEqual(positions.get("a").x, positions.get("b").x);
});
