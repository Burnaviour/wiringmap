"""Render the code graph as one self-contained, offline HTML file."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from wiringmap.diff import GraphDiff
from wiringmap.graph import CodeGraph, NodeKind
from wiringmap.parsers.python_ast import read_source

STATIC = Path(__file__).parent / "static"

# Load order matters: the layout plugins attach themselves to the cytoscape global.
VENDOR_SCRIPTS = (
    "htm-preact-standalone.umd.js",
    "cytoscape.min.js",
    "layout-base.js",
    "cose-base.js",
    "cytoscape-fcose.js",
)

# Embedding source lets the viewer show code, but it also grows the file; above this
# much Python, the report is written without source unless asked for explicitly.
SOURCE_BUDGET = 8_000_000

_SLOT = re.compile(r"<!--@@(\w+)@@-->")


def collect_sources(root: Path, graph: CodeGraph, limit: int | None = SOURCE_BUDGET) -> dict | None:
    """Python source of every module, keyed by path; None if it exceeds ``limit`` bytes."""
    sources: dict[str, str] = {}
    total = 0
    for node in graph.nodes.values():
        if node.kind != NodeKind.MODULE or not node.path:
            continue
        try:
            text = read_source((root / node.path).read_bytes())
        except (OSError, SyntaxError, UnicodeDecodeError, LookupError):
            continue
        total += len(text)
        if limit is not None and total > limit:
            return None
        sources[node.path] = text
    return sources


def render_report(
    graph: CodeGraph, sources: dict[str, str] | None = None, diff: GraphDiff | None = None
) -> str:
    """The report page; with ``diff``, the map also shows what changed."""
    template = (STATIC / "report.html").read_text(encoding="utf-8")
    payload = {"graph": graph.to_dict(), "sources": sources or {}}
    if diff is not None:
        payload["diff"] = diff.to_dict()
    slots = {
        "TITLE": html.escape(graph.root_name),
        "STYLE": (STATIC / "app.css").read_text(encoding="utf-8"),
        "VENDOR": "\n;\n".join(_script(STATIC / "vendor" / name) for name in VENDOR_SCRIPTS),
        "APP": _script(STATIC / "app.js"),
        "DATA": _json_for_script(payload),
    }
    # One pass, so text inserted into a slot is never itself scanned for slots.
    return _SLOT.sub(lambda m: slots[m.group(1)], template)


def _script(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("</script", "<\\/script")


def _json_for_script(data: object) -> str:
    """JSON that is safe inside <script>: no "</script>" or "<!--" can appear in it."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c")
