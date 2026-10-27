"""Follow the calls from one function: how it works (down) or how it is reached (up)."""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass, field

from wiringmap.diff import is_side_path
from wiringmap.graph import CodeGraph, Edge, EdgeKind, NodeKind

FLOW_KINDS = (NodeKind.MODULE, NodeKind.CLASS, NodeKind.FUNCTION, NodeKind.METHOD)
MAX_SIDE_STEPS = 3  # more test/example callers than this are folded into one line


@dataclass
class Step:
    id: str
    name: str  # "Coder.run"; a file's path for module-level code
    kind: str
    path: str | None = None
    line: int | None = None  # where it is defined
    call_line: int | None = None  # where the step above calls it (or, going up, where it calls)
    confidence: str | None = None  # how the link to the step above was found
    entry_point: str | None = None
    repeat: str | None = None  # "recursion" or "above": shown already, not expanded again
    more: int = 0  # further steps left out (depth or size limit)
    tests: int = 0  # steps in tests, examples or docs folded into one line
    external: list[str] = field(default_factory=list)  # calls into other packages
    children: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class FlowError(Exception):
    """A name that matches nothing or several things, explained for the user."""


def find(graph: CodeGraph, query: str) -> str:
    """The id of the function, method, class or file that ``query`` names.

    Accepts a full id (``shop.cart:Cart.total``), a qualified name (``Cart.total``), a
    plain name (``total``) or a module (``shop.cart``).
    """
    query = query.strip()
    if query in graph.nodes and graph.nodes[query].kind in FLOW_KINDS:
        return query
    candidates = [n for n in graph.nodes.values() if n.kind in FLOW_KINDS]
    for test in (
        lambda n: _qual(n.id) == query,
        lambda n: _dotted_match(n.id, query),
        lambda n: n.name == query and n.kind != NodeKind.MODULE,
        lambda n: _qual(n.id).endswith("." + query),
    ):
        matches = [n for n in candidates if test(n)]
        app = [n for n in matches if not is_side_path(n.path)]
        matches = app or matches  # prefer app code over tests with the same name
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            listed = "\n".join(
                f"  {n.id}  ({n.path}:{n.line})" if n.line else f"  {n.id}"
                for n in sorted(matches, key=lambda n: n.id)[:10]
            )
            more = f"\n  … and {len(matches) - 10} more" if len(matches) > 10 else ""
            raise FlowError(
                f"'{query}' matches {len(matches)} things. Use one of these full names:\n"
                f"{listed}{more}"
            )
    names = sorted({n.name for n in candidates} | {_qual(n.id) for n in candidates})
    close = difflib.get_close_matches(query, names, n=5, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise FlowError(f"No function, class or file named '{query}' was found.{hint}")


def build_flow(
    graph: CodeGraph,
    root: str,
    direction: str = "down",
    depth: int = 2,
    limit: int = 150,
    per_step: int = 25,
) -> Step:
    """The call tree under ``root`` (``down``: what it calls) or above it (``up``: callers).

    Steps come in the order the calls appear in the code. A function that already
    appeared is shown again only as a pointer, and recursion is marked rather than
    followed, so the tree stays finite. ``more`` counts what the limits left out.
    """
    if direction not in ("down", "up"):
        raise ValueError("direction must be 'down' or 'up'")
    calls_from: dict[str, list[Edge]] = {}
    calls_to: dict[str, list[Edge]] = {}
    for edge in graph.edges.values():
        if edge.kind == EdgeKind.CALLS and edge.source != edge.target:
            calls_from.setdefault(edge.source, []).append(edge)
            calls_to.setdefault(edge.target, []).append(edge)

    def _other(edge: Edge) -> str:
        return edge.target if direction == "down" else edge.source

    def neighbours(node_id: str) -> list[Edge]:
        if direction == "down":
            return sorted(calls_from.get(node_id, []), key=lambda e: (e.line or 0, e.target))
        callers = calls_to.get(node_id, [])
        return sorted(callers, key=lambda e: (is_side_path(graph.nodes[e.source].path), e.source))

    shown: set[str] = set()
    budget = [limit]

    def visit(node_id: str, edge: Edge | None, level: int, path: tuple[str, ...]) -> Step:
        step = _step(graph, node_id, edge, direction)
        if node_id in path:
            step.repeat = "recursion"
            return step
        if node_id in shown:
            step.repeat = "above"
            return step
        shown.add(node_id)
        budget[0] -= 1
        nexts = neighbours(node_id)
        side = [e for e in nexts if is_side_path(graph.nodes[_other(e)].path)]
        # Twenty test functions calling this would bury the two app callers that matter.
        # At the top, keep them when tests are all there is.
        if len(side) > MAX_SIDE_STEPS and (level > 0 or len(side) < len(nexts)):
            nexts = [e for e in nexts if e not in side]
            step.tests = len(side)
        if level >= depth or budget[0] <= 0:
            step.more = len(nexts)
            return step
        for i, e in enumerate(nexts):
            if i >= per_step or budget[0] <= 0:
                step.more = len(nexts) - i
                break
            step.children.append(visit(_other(e), e, level + 1, (*path, node_id)))
        return step

    return visit(root, None, 0, ())


def render_text(root: Step, direction: str = "down") -> str:
    """The tree as indented text, ready for a terminal or a Markdown code block."""
    lines = [_label(root, top=True)]
    if root.external and direction == "down":
        lines.append(f"   uses {', '.join(root.external)}")
    _render(root.children, "", lines, direction, trailing=bool(root.more or root.tests))
    if root.children:
        _tail(root, "", lines)
    if not root.children and not root.tests:
        if root.more:
            lines.append("└─ … (raise --depth to see more)")
        elif direction == "down":
            lines.append("   (calls nothing else in this project)")
        else:
            lines.append("   (nothing in this project calls it; a framework or a user may)")
    return "\n".join(lines) + "\n"


def _render(
    steps: list[Step], prefix: str, lines: list[str], direction: str, trailing: bool = False
) -> None:
    """``trailing``: a summary line follows these steps, so none of them is the last."""
    for i, step in enumerate(steps):
        last = i == len(steps) - 1 and not trailing
        lines.append(prefix + ("└─ " if last else "├─ ") + _label(step))
        inner = prefix + ("   " if last else "│  ")
        if step.external and direction == "down" and not step.repeat:
            lines.append(inner + f"uses {', '.join(step.external)}")
        _render(step.children, inner, lines, direction, trailing=bool(step.more or step.tests))
        _tail(step, inner, lines)


def _tail(step: Step, prefix: str, lines: list[str]) -> None:
    if step.repeat:
        return
    if step.more:
        word = "step" if step.more == 1 else "steps"
        lines.append(prefix + f"└─ … {step.more} more {word}")
    if step.tests:
        lines.append(prefix + f"└─ … and {step.tests} in tests, examples or docs")


def _label(step: Step, top: bool = False) -> str:
    where = f"{step.path}:{step.line}" if step.path and step.line else step.path or ""
    text = step.name + (" (class)" if step.kind == NodeKind.CLASS else "")
    if top:
        text += f"  ({where})" if where else ""
    else:
        text += f"  {where}" if where else ""
    if step.confidence and step.confidence != "resolved":
        text += f"  [{step.confidence}]"
    if step.entry_point:
        text += f"  · starts here: {step.entry_point}"
    if step.repeat == "recursion":
        text += "  ↻ recursion"
    elif step.repeat == "above":
        text += "  (shown above)"
    return text


def _step(graph: CodeGraph, node_id: str, edge: Edge | None, direction: str) -> Step:
    node = graph.nodes[node_id]
    name = node.path or node.id if node.kind == NodeKind.MODULE else _qual(node.id)
    return Step(
        id=node_id,
        name=name,
        kind=node.kind,
        path=node.path,
        line=node.line,
        call_line=edge.line if edge else None,
        confidence=edge.confidence if edge else None,
        entry_point=node.attrs.get("entry_point"),
        external=list(node.attrs.get("external_calls", [])) if direction == "down" else [],
    )


def _dotted_match(node_id: str, query: str) -> bool:
    """``crud.create_user`` or ``app.crud.create_user`` naming ``app.crud:create_user``."""
    module, _, qual = node_id.partition(":")
    if not qual:
        return module == query or module.endswith("." + query)
    parts = query.split(".")
    for i in range(1, len(parts)):
        mod_part, qual_part = ".".join(parts[:i]), ".".join(parts[i:])
        if qual == qual_part and (module == mod_part or module.endswith("." + mod_part)):
            return True
    return False


def _qual(node_id: str) -> str:
    return node_id.partition(":")[2] or node_id
