"""Turn a GraphDiff into a Markdown summary for people, pull requests and AI agents."""

from __future__ import annotations

from collections import defaultdict

from wiringmap.diff import GraphDiff, Item, SignatureChange, is_side_path
from wiringmap.graph import CodeGraph, NodeKind

NAMES_PER_LINE = 8  # a file that gained 40 tests should not become a wall of names

KIND_WORDS = {
    NodeKind.MODULE: "files",
    NodeKind.CLASS: "classes",
    NodeKind.FUNCTION: "functions",
    NodeKind.METHOD: "methods",
}


def to_markdown(d: GraphDiff, graph: CodeGraph | None = None, limit: int = 12) -> str:
    """The diff as Markdown. ``graph`` (the newer side) turns ids into file paths."""
    out = [f"## wiringmap diff: `{d.base}` → `{d.head}`", ""]
    if d.empty:
        out.append("No changes to Python code.")
        return "\n".join(out) + "\n"
    out += [_headline(d), ""]
    attention = _attention(d, graph, limit)
    if attention:
        out += ["### Needs a look", "", *attention, ""]
    out += ["### Changes by file", "", *_changes(d, limit * 2)]
    structure = _structure(d, graph, limit)
    if structure:
        out += ["", "### Structure", "", *structure]
    return "\n".join(out) + "\n"


def _headline(d: GraphDiff) -> str:
    counts = d.counts()
    parts = []
    for kind, word in KIND_WORDS.items():
        bits = [
            f"{counts[status][kind]} {status}"
            for status in ("added", "changed", "removed")
            if counts[status].get(kind)
        ]
        if bits:
            parts.append(f"{word}: " + ", ".join(bits))
    moved = sum(counts["moved"].values())
    if moved:
        parts.append(f"{moved} renamed or moved")
    return "**" + " · ".join(parts) + "**"


def _attention(d: GraphDiff, graph: CodeGraph | None, limit: int) -> list[str]:
    lines = []
    for item in d.broken:
        lines.append(
            f"- **No longer parses:** `{item.path}`: {item.note}. "
            "Its classes and functions cannot be checked until it does."
        )
    for cycle in d.new_cycles:
        loop = " → ".join(_path(graph, m) for m in [*cycle, cycle[0]])
        lines.append(f"- **New import cycle:** {loop}")
    for change in d.signature_changes:
        if change.callers and change.breaking is not False:
            lines.append(_signature_warning(change, graph, limit))
    for dup in d.duplicates:
        new = [m for m in dup.members if m.id in dup.new]
        old = [m for m in dup.members if m.id not in dup.new]
        copies = ", ".join(_where(m) for m in new)
        if old:
            lines.append(
                f"- **Same logic twice:** {copies} repeats {', '.join(_where(m) for m in old)}"
            )
        else:
            lines.append(f"- **Same logic twice:** {copies}")
    for item in d.lost_callers:
        lines.append(f"- **Lost all its callers:** {_where(item)} ({item.note}, none now)")
    for pkg in d.new_packages:
        lines.append(f"- **New third-party package:** `{pkg.name}` (imported by {pkg.note})")
    return lines


def _signature_warning(change: SignatureChange, graph: CodeGraph | None, limit: int) -> str:
    app = [c for c in change.callers if not is_side_path(c.path)]
    tests = len(change.callers) - len(app)
    verdict = "old calls will fail" if change.breaking else "old calls may fail"
    where = []
    if app:
        where.append(_list([_caller(c) for c in app], limit))
    if tests:
        where.append(f"{tests} test(s)")
    location = f" ({change.path})" if change.path else ""
    return (
        f"- **Signature changed, {verdict}:** `{_short(change.id)}`{location} "
        f"`{change.before}` → `{change.after}`. Not updated: {' and '.join(where)}"
    )


def _caller(item: Item) -> str:
    if item.kind == NodeKind.MODULE:
        return f"`{item.path}` (top level)"
    return _where(item)


def _changes(d: GraphDiff, limit: int) -> list[str]:
    """One line per file: what happened to the file and to the code inside it."""
    files: dict[str, dict] = defaultdict(lambda: {"file": None, "symbols": defaultdict(list)})
    for status in ("added", "changed", "removed"):
        listed = {item.id for item in getattr(d, status)}
        for item in getattr(d, status):
            entry = files[item.path or item.id]
            if item.kind == NodeKind.MODULE:
                entry["file"] = status
            elif not _inside_listed(item.id, listed):
                entry["symbols"][status].append(f"`{_short(item.id)}`")
    for item in d.moved:
        entry = files[item.path or item.id]
        if item.kind == NodeKind.MODULE:
            entry["file"] = "renamed"
            entry["from"] = item.old_path
        elif item.old_path == item.path:
            entry["symbols"]["renamed"].append(f"`{_short(item.note or '')}` → `{_short(item.id)}`")
        else:
            entry["symbols"]["moved here"].append(f"`{_short(item.id)}` (from `{item.old_path}`)")
    broken = {item.path for item in d.broken}

    lines = []
    # New files, then app code, then tests, examples and docs.
    order = sorted(files, key=lambda p: (is_side_path(p), files[p]["file"] != "added", p))
    for path in order:
        entry = files[path]
        label = {
            "added": " (new file)",
            "removed": " (deleted)",
            "renamed": f" (renamed from `{entry.get('from')}`)",
        }.get(entry["file"] or "", "")
        if path in broken:
            label += " (no longer parses)"
        details = [
            f"{status} {_list(names, NAMES_PER_LINE)}"
            for status in ("added", "changed", "removed", "renamed", "moved here")
            if (names := entry["symbols"][status])
        ]
        if not details and entry["file"] == "changed" and path not in broken:
            details = ["imports or top-level code"]
        lines.append(f"- `{path}`{label}" + (": " + "; ".join(details) if details else ""))
    if len(lines) > limit:
        lines = [*lines[:limit], f"- and {len(lines) - limit} more files"]
    return lines


def _structure(d: GraphDiff, graph: CodeGraph | None, limit: int) -> list[str]:
    lines = []
    if d.new_dependencies:
        deps = [f"{_path(graph, a)} → {_path(graph, b)}" for a, b in d.new_dependencies]
        lines.append("- **New dependencies between existing files:** " + _list(deps, limit))
    if d.impacted:
        app = [p for p in d.impacted if not is_side_path(p)]
        side = len(d.impacted) - len(app)
        extra = f" (plus {side} test, example or docs file(s))" if side else ""
        if app:
            lines.append(
                f"- **{len(app)} other file(s) import the changed files{extra}:** "
                + _list([f"`{p}`" for p in app], limit)
            )
        else:
            lines.append(f"- **Only tests, examples or docs import the changed files**{extra}")
    if d.unused_new:
        lines.append(
            "- **New functions nothing calls yet** (a framework may call them): "
            + _list([_where(i) for i in d.unused_new], limit)
        )
    compatible = [c for c in d.signature_changes if c.breaking is False]
    if compatible:
        sigs = [f"{_symbol(graph, c.id)} `{c.before}` → `{c.after}`" for c in compatible]
        lines.append(
            "- **Signatures changed, same parameters or new optional ones:** " + _list(sigs, limit)
        )
    uncalled = [c for c in d.signature_changes if c.breaking is not False and not c.callers]
    if uncalled:
        sigs = [f"{_symbol(graph, c.id)} `{c.before}` → `{c.after}`" for c in uncalled]
        lines.append("- **Signatures changed, no callers found to update:** " + _list(sigs, limit))
    if d.resolved_cycles:
        loops = [" ↔ ".join(_path(graph, m) for m in c) for c in d.resolved_cycles]
        lines.append("- **Import cycles removed:** " + _list(loops, limit))
    if d.repaired:
        lines.append(
            "- **Files that parse again:** " + _list([f"`{i.path}`" for i in d.repaired], limit)
        )
    if d.dropped_packages:
        lines.append(
            "- **Packages no longer imported:** "
            + _list([f"`{p}`" for p in d.dropped_packages], limit)
        )
    return lines


# Formatting helpers -------------------------------------------------------------------


def _inside_listed(node_id: str, listed: set[str]) -> bool:
    """``mod:test_x.helper`` is noise when ``mod:test_x`` is already in the same list."""
    module, _, qual = node_id.partition(":")
    parts = qual.split(".")
    return any(f"{module}:{'.'.join(parts[:i])}" in listed for i in range(1, len(parts)))


def _short(node_id: str) -> str:
    """``shop.cart:Cart.total`` -> ``Cart.total``; a module id stays as it is."""
    return node_id.partition(":")[2] or node_id


def _where(item: Item) -> str:
    if item.kind == NodeKind.MODULE:
        return f"`{item.path or item.id}`"
    location = f"{item.path}:{item.line}" if item.path and item.line else item.path or ""
    return f"`{_short(item.id)}` ({location})" if location else f"`{_short(item.id)}`"


def _node_path(graph: CodeGraph | None, node_id: str) -> str | None:
    node = graph.nodes.get(node_id) if graph else None
    return node.path if node else None


def _path(graph: CodeGraph | None, module_id: str) -> str:
    return f"`{_node_path(graph, module_id) or module_id}`"


def _symbol(graph: CodeGraph | None, node_id: str) -> str:
    path = _node_path(graph, node_id)
    return f"`{_short(node_id)}` ({path})" if path else f"`{_short(node_id)}`"


def _list(items: list[str], limit: int) -> str:
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", and {len(items) - limit} more"
