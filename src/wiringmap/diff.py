"""Compare two code graphs: what changed in the structure, not just in the text."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from wiringmap.graph import CodeGraph, EdgeKind, Node, NodeKind

TRACKED = (NodeKind.MODULE, NodeKind.CLASS, NodeKind.FUNCTION, NodeKind.METHOD)
SYMBOLS = (NodeKind.CLASS, NodeKind.FUNCTION, NodeKind.METHOD)

# Tests, docs, examples and migrations: real code, but duplicates and unused functions
# there are normal (similar tests, fixtures called by the test runner).
_SIDE_FOLDER = re.compile(
    r"^(tests?|testing|fixtures|examples?|docs?|benchmarks?|__snapshots__|migrations?|alembic)$",
    re.IGNORECASE,
)
_TEST_FILE = re.compile(r"^(test_.*|.*_test|conftest)\.py$")


def is_side_path(path: str | None) -> bool:
    parts = (path or "").split("/")
    return any(_SIDE_FOLDER.match(p) for p in parts[:-1]) or bool(_TEST_FILE.match(parts[-1]))


@dataclass
class Item:
    id: str
    kind: str
    name: str
    path: str | None = None
    line: int | None = None
    note: str | None = None
    old_path: str | None = None  # for renamed or moved items: where it was


@dataclass
class SignatureChange:
    id: str
    path: str | None
    before: str
    after: str
    callers: list[Item] = field(default_factory=list)  # still calling it, unchanged themselves
    updated_callers: list[Item] = field(default_factory=list)  # changed in the same diff
    breaking: bool | None = None  # can old-style calls fail? None when we cannot tell


@dataclass
class Duplicate:
    members: list[Item]  # same structure; at least one is new or changed
    new: list[str]  # ids of the members this diff added or changed


@dataclass
class GraphDiff:
    base: str
    head: str
    added: list[Item] = field(default_factory=list)
    removed: list[Item] = field(default_factory=list)
    changed: list[Item] = field(default_factory=list)
    moved: list[Item] = field(default_factory=list)  # note: the old id
    broken: list[Item] = field(default_factory=list)  # files that stopped parsing
    repaired: list[Item] = field(default_factory=list)  # files that parse again
    new_cycles: list[list[str]] = field(default_factory=list)
    resolved_cycles: list[list[str]] = field(default_factory=list)
    new_packages: list[Item] = field(default_factory=list)  # note: which files import it
    dropped_packages: list[str] = field(default_factory=list)
    new_dependencies: list[tuple[str, str]] = field(default_factory=list)  # existing files only
    dropped_dependencies: list[tuple[str, str]] = field(default_factory=list)
    signature_changes: list[SignatureChange] = field(default_factory=list)
    lost_callers: list[Item] = field(default_factory=list)
    unused_new: list[Item] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)
    impacted: list[str] = field(default_factory=list)  # files depending on what changed

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.moved or self.broken)

    def counts(self) -> dict[str, dict[str, int]]:
        table: dict[str, dict[str, int]] = {}
        for status in ("added", "removed", "changed", "moved"):
            per_kind: dict[str, int] = defaultdict(int)
            for item in getattr(self, status):
                per_kind[item.kind] += 1
            table[status] = dict(per_kind)
        return table

    def to_dict(self) -> dict:
        data = asdict(self)
        data["counts"] = self.counts()
        return data


def diff_graphs(before: CodeGraph, after: CodeGraph, base: str, head: str) -> GraphDiff:
    return _Differ(before, after).run(GraphDiff(base=base, head=head))


class _Differ:
    def __init__(self, before: CodeGraph, after: CodeGraph) -> None:
        self.before = before
        self.after = after
        self.old = {i: n for i, n in before.nodes.items() if n.kind in TRACKED}
        self.new = {i: n for i, n in after.nodes.items() if n.kind in TRACKED}
        self.renamed: dict[str, str] = {}  # old id -> new id, for modules and symbols

    def run(self, d: GraphDiff) -> GraphDiff:
        # A file that does not parse on one side hides its classes and functions there.
        # They are unknown, not added or removed.
        self.unreadable = {
            nid
            for graph in (self.before, self.after)
            for nid, node in graph.nodes.items()
            if node.kind == NodeKind.MODULE and node.attrs.get("parse_error")
        }
        added_ids = {i for i in set(self.new) - set(self.old) if not self._hidden(i)}
        removed_ids = {i for i in set(self.old) - set(self.new) if not self._hidden(i)}
        self._match_moves(added_ids, removed_ids, d)

        for nid in sorted(added_ids):
            d.added.append(_item(self.new[nid]))
        for nid in sorted(removed_ids):
            d.removed.append(_item(self.old[nid]))
        changed_ids = self._changed_ids()
        for nid in sorted(changed_ids):
            d.changed.append(_item(self.new[nid]))

        self.old_id_of = {new: old for old, new in self.renamed.items()}
        touched = changed_ids | added_ids | set(self.renamed.values())
        self._parse_health(d)
        self._dependencies(d)
        self._packages(d)
        self._signatures(d, changed_ids, touched)
        self._callers(d, added_ids)
        self._duplicates(d, changed_ids | added_ids)  # a pure move duplicates nothing new
        self._impact(d, changed_ids | added_ids | removed_ids)
        return d

    # Moves and renames ---------------------------------------------------------------

    def _match_moves(self, added: set[str], removed: set[str], d: GraphDiff) -> None:
        """Pair a removed and an added node with identical code: a rename or a move."""
        by_hash: dict[tuple[str, str], list[str]] = defaultdict(list)
        for nid in added:
            node = self.new[nid]
            if node.attrs.get("hash"):
                by_hash[(node.kind, node.attrs["hash"])].append(nid)
        pairs = []
        for old_id in sorted(removed):
            node = self.old[old_id]
            candidates = by_hash.get((node.kind, node.attrs.get("hash", "")), [])
            if len(candidates) == 1:  # ambiguous matches stay an add plus a remove
                pairs.append((old_id, candidates.pop()))
        moved_modules = {o: n for o, n in pairs if self.old[o].kind == NodeKind.MODULE}
        for old_id, new_id in pairs:
            self.renamed[old_id] = new_id
            added.discard(new_id)
            removed.discard(old_id)
            old_mod, _, old_qual = old_id.partition(":")
            new_mod, _, new_qual = new_id.partition(":")
            if old_qual and moved_modules.get(old_mod) == new_mod and old_qual == new_qual:
                continue  # it moved with its file; the file move says it all
            moved = _item(self.new[new_id], note=old_id)
            moved.old_path = self.old[old_id].path
            d.moved.append(moved)
        # Symbols inside a moved file keep their names but change ids: pair them up too.
        for old_mod, new_mod in moved_modules.items():
            for old_id in sorted(removed):
                mod, _, qual = old_id.partition(":")
                if mod == old_mod and qual and f"{new_mod}:{qual}" in added:
                    self.renamed[old_id] = f"{new_mod}:{qual}"
                    added.discard(f"{new_mod}:{qual}")
                    removed.discard(old_id)

    def _changed_ids(self) -> set[str]:
        changed = set()
        pairs = [(i, i) for i in set(self.old) & set(self.new)] + list(self.renamed.items())
        for old_id, new_id in pairs:
            a, b = self.old[old_id].attrs.get("hash"), self.new[new_id].attrs.get("hash")
            if a and b and a != b:
                changed.add(new_id)
        # A class's fingerprint covers its methods; report the methods, not the class too.
        for nid in list(changed):
            if self.new[nid].kind == NodeKind.CLASS and any(
                c.startswith(nid + ".") for c in changed | (set(self.new) - set(self.old))
            ):
                changed.discard(nid)
        # A file always changes when something in it does; keep files in the list anyway,
        # they are what people scan first.
        return changed

    # Parsing ---------------------------------------------------------------------------

    def _parse_health(self, d: GraphDiff) -> None:
        for nid, node in sorted(self.new.items()):
            if node.kind != NodeKind.MODULE:
                continue
            old = self.old.get(self._old_id(nid))
            error = node.attrs.get("parse_error")
            old_error = old.attrs.get("parse_error") if old else None
            if error and not old_error:
                d.broken.append(_item(node, note=error))
            elif old_error and not error and old is not None:
                d.repaired.append(_item(node))

    # Dependencies between files -------------------------------------------------------

    def _imports(self, graph: CodeGraph, translate: bool) -> set[tuple[str, str]]:
        pairs = set()
        for e in graph.edges.values():
            if e.kind != EdgeKind.IMPORTS or e.attrs.get("type_only"):
                continue
            src, dst = graph.nodes.get(e.source), graph.nodes.get(e.target)
            if not (src and dst and src.kind == NodeKind.MODULE and dst.kind == NodeKind.MODULE):
                continue
            if translate:
                pairs.add(
                    (self.renamed.get(e.source, e.source), self.renamed.get(e.target, e.target))
                )
            else:
                pairs.add((e.source, e.target))
        return pairs

    def _dependencies(self, d: GraphDiff) -> None:
        before = self._imports(self.before, translate=True)
        after = self._imports(self.after, translate=False)
        existed = {self.renamed.get(i, i) for i in self.old}
        still = set(self.new)
        d.new_dependencies = sorted(
            p for p in after - before if p[0] in existed and p[1] in existed
        )
        d.dropped_dependencies = sorted(
            p for p in before - after if p[0] in still and p[1] in still
        )
        old_cycles = {frozenset(c) for c in _cycles(before)}
        new_cycles = {frozenset(c) for c in _cycles(after)}
        d.new_cycles = [sorted(c) for c in sorted(new_cycles - old_cycles, key=sorted)]
        d.resolved_cycles = [sorted(c) for c in sorted(old_cycles - new_cycles, key=sorted)]

    def _packages(self, d: GraphDiff) -> None:
        def third_party(graph: CodeGraph) -> dict[str, list[str]]:
            users: dict[str, list[str]] = defaultdict(list)
            for e in graph.edges.values():
                target = graph.nodes.get(e.target)
                if (
                    e.kind == EdgeKind.IMPORTS
                    and target
                    and target.kind == NodeKind.EXTERNAL
                    and not target.attrs.get("stdlib")
                ):
                    users[target.name].append(e.source)
            return users

        old, new = third_party(self.before), third_party(self.after)
        for name in sorted(set(new) - set(old)):
            importers = sorted(self.after.nodes[m].path or m for m in new[name])
            d.new_packages.append(
                Item(id=f"ext:{name}", kind=NodeKind.EXTERNAL, name=name, note=", ".join(importers))
            )
        d.dropped_packages = sorted(set(old) - set(new))

    # Calls -----------------------------------------------------------------------------

    def _callers(self, d: GraphDiff, added: set[str]) -> None:
        old_in = _callers_of(self.before)
        new_in = _callers_of(self.after)
        # Calls the analysis could not link, by name. A function whose name appears here
        # may still be called from anywhere, so "lost all its callers" would be a guess.
        unlinked = {
            name
            for node in self.after.nodes.values()
            for name in node.attrs.get("unresolved_names", ())
        }
        for old_id in sorted(self.old):
            new_id = self.renamed.get(old_id, old_id)
            new_node = self.new.get(new_id)
            if not new_node or new_node.kind not in (NodeKind.FUNCTION, NodeKind.METHOD):
                continue
            old_callers = old_in.get(old_id, set())
            if any(self._hidden(c) or c in self.unreadable for c in old_callers):
                continue  # its callers are in a file we cannot read right now
            if new_node.name in unlinked or any(
                self._may_still_call(c, new_node.name) for c in old_callers
            ):
                continue
            before_count = len(old_callers)
            if before_count and not new_in.get(new_id) and not new_node.attrs.get("entry_point"):
                d.lost_callers.append(
                    _item(new_node, note=f"called from {before_count} place(s) before")
                )
        for nid in sorted(added):
            node = self.new[nid]
            qual = nid.partition(":")[2]
            if (
                node.kind == NodeKind.FUNCTION
                and "." not in qual  # module-level; methods are often called by frameworks
                and not new_in.get(nid)
                and not node.attrs.get("entry_point")
                and not node.attrs.get("decorators")  # @app.route, @pytest.fixture, ...
                and not is_side_path(node.path)
                and not qual.startswith("__")
            ):
                d.unused_new.append(_item(node))

    def _signatures(self, d: GraphDiff, changed: set[str], touched: set[str]) -> None:
        new_in = _callers_of(self.after)
        touched_modules = {t.partition(":")[0] for t in touched}
        for new_id in sorted(changed):
            node = self.new[new_id]
            old = self.old.get(self._old_id(new_id))
            if not old or not node.signature or old.signature == node.signature:
                continue
            callers = sorted(new_in.get(new_id, ()))
            change = SignatureChange(
                id=new_id,
                path=node.path,
                before=old.signature or "",
                after=node.signature,
                breaking=breaking_change(old.signature or "", node.signature),
            )
            for caller in callers:
                item = _item(self.after.nodes[caller])
                caller_changed = caller in touched or (
                    ":" not in caller and caller in touched_modules
                )
                (change.updated_callers if caller_changed else change.callers).append(item)
            d.signature_changes.append(change)

    # Duplicates ------------------------------------------------------------------------

    def _duplicates(self, d: GraphDiff, touched: set[str]) -> None:
        groups: dict[str, list[Node]] = defaultdict(list)
        for node in self.new.values():
            shape = node.attrs.get("shape")
            if shape and not is_side_path(node.path):
                groups[shape].append(node)
        for members in groups.values():
            if len(members) < 2:
                continue
            new_ids = sorted(m.id for m in members if m.id in touched)
            if new_ids:
                ordered = sorted(members, key=lambda m: (m.id not in new_ids, m.id))
                d.duplicates.append(Duplicate(members=[_item(m) for m in ordered], new=new_ids))
        d.duplicates.sort(key=lambda g: (-len(g.members), g.members[0].id))

    # Impact ----------------------------------------------------------------------------

    def _impact(self, d: GraphDiff, touched: set[str]) -> None:
        """Files that import, directly or not, a file this diff changed."""
        modules = {t.partition(":")[0] for t in touched}
        importers: dict[str, set[str]] = defaultdict(set)
        for src, dst in self._imports(self.after, translate=False):
            importers[dst].add(src)
        seen, frontier = set(), set(modules)
        while frontier:
            frontier = {i for m in frontier for i in importers.get(m, ())} - seen - modules
            seen |= frontier
        d.impacted = sorted(self.after.nodes[m].path or m for m in seen if m in self.after.nodes)

    def _may_still_call(self, old_caller: str, name: str) -> bool:
        """Whether a former caller probably still makes the call, just not linked now.

        Analysis can lose a link when the calling code is reshaped (for example
        ``x = ctx.copy()`` becoming ``with original.copy() as x``). Claiming "lost all
        its callers" then would be wrong.
        """
        caller = self.new.get(self.renamed.get(old_caller, old_caller))
        if caller is None:
            return False  # the caller is gone, so the call is gone
        before = self.old.get(old_caller)
        if before and before.attrs.get("hash") == caller.attrs.get("hash"):
            return True  # unchanged code still makes the call
        return name in caller.attrs.get("unresolved_names", ())

    def _hidden(self, node_id: str) -> bool:
        module, _, qual = node_id.partition(":")
        return bool(qual) and module in self.unreadable

    def _old_id(self, new_id: str) -> str:
        return self.old_id_of.get(new_id, new_id)


def _item(node: Node, note: str | None = None) -> Item:
    return Item(
        id=node.id, kind=node.kind, name=node.name, path=node.path, line=node.line, note=note
    )


def _callers_of(graph: CodeGraph) -> dict[str, set[str]]:
    callers: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if e.kind in (EdgeKind.CALLS, EdgeKind.DECORATES) and e.source != e.target:
            callers[e.target].add(e.source)
    return callers


def _cycles(pairs: set[tuple[str, str]]) -> list[list[str]]:
    """Strongly connected groups of two or more files (Tarjan, without recursion)."""
    graph: dict[str, list[str]] = defaultdict(list)
    for src, dst in pairs:
        if src != dst:
            graph[src].append(dst)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    group = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.append(member)
                        if member == node:
                            break
                    if len(group) > 1:
                        found.append(sorted(group))
    return found


def breaking_change(before: str, after: str) -> bool | None:
    """Whether calls written for ``before`` can fail against ``after``.

    Compatible: new parameters with defaults, a new ``*args`` or ``**kwargs``, or a
    different return annotation. Breaking: removing, renaming or reordering positional
    parameters, removing a keyword-only one, or adding any parameter without a default.
    None when either signature cannot be read.
    """
    old, new = _params(before), _params(after)
    if old is None or new is None:
        return None
    old_pos, old_kwonly, _, _ = old
    new_pos, new_kwonly, _, new_varkw = new
    if [name for name, _ in new_pos[: len(old_pos)]] != [name for name, _ in old_pos]:
        return True
    if any(not has_default for _, has_default in new_pos[len(old_pos) :]):
        return True
    old_kw = {name for name, _ in old_kwonly}
    new_kw = dict(new_kwonly)
    if not new_varkw and any(name not in new_kw and name not in dict(new_pos) for name in old_kw):
        return True
    return any(not has_default for name, has_default in new_kw.items() if name not in old_kw)


def _params(signature: str):
    """(positional, keyword-only, has *args, has **kwargs); each param is (name, has_default)."""
    import ast

    head = signature.split(" -> ", 1)[0]
    try:
        args = ast.parse(f"def f{head}: pass").body[0].args  # type: ignore[attr-defined]
    except (SyntaxError, IndexError, AttributeError):
        return None
    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    pos = [(a.arg, i >= first_default) for i, a in enumerate(positional)]
    kwonly = [
        (a.arg, d is not None) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    ]
    return pos, kwonly, args.vararg is not None, args.kwarg is not None
