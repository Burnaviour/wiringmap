"""Link extracted Python modules into the code graph.

Static analysis of Python is best effort. Every call edge says how it was found
(see :class:`wiringmap.graph.Confidence`), and calls that cannot be linked are counted
on the calling function instead of being drawn.
"""

from __future__ import annotations

import builtins
import posixpath
import sys
from collections import defaultdict
from dataclasses import dataclass

from wiringmap.graph import CodeGraph, Confidence, Edge, EdgeKind, Node, NodeKind
from wiringmap.parsers.python_ast import (
    ClassInfo,
    FunctionInfo,
    ImportBinding,
    ModuleInfo,
    Parts,
)

BUILTINS = frozenset(dir(builtins))
STDLIB = frozenset(sys.stdlib_module_names)
MAX_EXTERNAL_CALLS = 30
MAX_UNRESOLVED_NAMES = 40
MAX_RESOLVE_DEPTH = 12

# A call like ``obj.<name>()`` on an object of unknown type is linked to the only
# method with that name in the project, but never for names this generic.
_GENERIC_METHOD_NAMES = frozenset(
    name
    for t in (object, str, bytes, int, float, list, dict, set, frozenset, tuple)
    for name in dir(t)
) | {
    "add", "apply", "build", "call", "close", "connect", "create", "debug", "delete",
    "dump", "emit", "error", "exception", "execute", "filter", "flush", "get", "handle",
    "info", "load", "log", "open", "parse", "process", "read", "remove", "render", "reset",
    "run", "save", "send", "set", "setup", "start", "stop", "update", "validate", "warning",
    "write",
}  # fmt: skip

_HTTP_DECORATORS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "route", "websocket", "api_route"}
)
_CLI_DECORATORS = frozenset({"command", "group", "callback"})


@dataclass
class PyModule:
    id: str  # dotted module name, unique in the graph
    path: str  # repository-relative file path
    path_name: str  # dotted path from the repository root, e.g. "src.shop.models"
    is_package: bool
    loose: bool  # a plain script: neither a package nor inside one
    info: ModuleInfo


@dataclass(frozen=True)
class Target:
    """What a name refers to."""

    kind: str  # "module", "symbol", "variable", "external" or "builtin"
    ref: str  # module id, symbol id, "<module>:<name>", dotted external name or builtin name


@dataclass(frozen=True)
class Ctx:
    """A place in the code where names are looked up: a module plus a scope qualname."""

    module: PyModule
    qualname: str


@dataclass(frozen=True)
class ModuleName:
    id: str
    path_name: str
    is_package: bool
    loose: bool


def assign_module_names(paths: list[str], root_name: str) -> dict[str, ModuleName]:
    """Map each Python file path to its module id and naming facts.

    The id is the import name Python would use. It starts at the topmost directory
    with an ``__init__.py``; folders without one below that are namespace packages
    and still count. Files with no package above them are loose scripts named by
    their stem. When two files would get the same id (two ``scripts/run.py`` style
    files), they fall back to their path-derived names.
    """
    init_dirs = {posixpath.dirname(p) for p in paths if posixpath.basename(p) == "__init__.py"}
    result: dict[str, ModuleName] = {}
    for path in paths:
        directory, filename = posixpath.split(path)
        stem = filename[: -len(".py")]
        is_package = stem == "__init__"
        top = _topmost_package_dir(directory, init_dirs)
        if top is None:
            parts = [stem]
        elif top == "":  # the scanned folder is itself a package, e.g. site-packages/flask
            parts = [root_name, *(directory.split("/") if directory else [])]
        else:
            parts = posixpath.relpath(directory, posixpath.dirname(top) or ".").split("/")
        if top is not None and not is_package:
            parts.append(stem)
        path_parts = path[: -len(".py")].split("/")
        if is_package:
            path_parts = path_parts[:-1] or [root_name]
        result[path] = ModuleName(".".join(parts), ".".join(path_parts), is_package, top is None)

    by_id: dict[str, list[str]] = defaultdict(list)
    for path, name in result.items():
        by_id[name.id].append(path)
    taken = {mod_id for mod_id, owners in by_id.items() if len(owners) == 1}
    for owners in by_id.values():
        if len(owners) == 1:
            continue
        for path in sorted(owners, key=lambda p: (not result[p].is_package, p)):
            name = result[path]
            candidate, n = name.path_name, 2
            while candidate in taken:
                candidate, n = f"{name.path_name}#{n}", n + 1
            taken.add(candidate)
            result[path] = ModuleName(candidate, name.path_name, name.is_package, name.loose)
    return result


def _topmost_package_dir(directory: str, init_dirs: set[str]) -> str | None:
    """The highest ancestor of ``directory`` (itself included) that has an ``__init__.py``."""
    top, d = None, directory
    while True:
        if d in init_dirs:
            top = d
        if not d:
            return top
        d = posixpath.dirname(d)


class PythonResolver:
    def __init__(self, modules: list[PyModule], graph: CodeGraph) -> None:
        self.graph = graph
        self.modules = {m.id: m for m in modules}
        self.by_path_name = {m.path_name: m.id for m in modules}
        # When the scanned folder is a package itself, paths from it are not import names.
        self.root_is_package = any(m.path == "__init__.py" for m in modules)
        self.aliases = self._build_aliases(modules)
        self.classes: dict[str, tuple[PyModule, ClassInfo]] = {}
        self.functions: dict[str, tuple[PyModule, FunctionInfo]] = {}
        methods_by_name: dict[str, list[str]] = defaultdict(list)
        for m in modules:
            for qual, cls in m.info.classes.items():
                self.classes[f"{m.id}:{qual}"] = (m, cls)
            for qual, func in m.info.functions.items():
                self.functions[f"{m.id}:{qual}"] = (m, func)
                if func.is_method:
                    methods_by_name[func.name].append(f"{m.id}:{qual}")
        self.unique_methods = {
            name: ids[0]
            for name, ids in methods_by_name.items()
            if len(ids) == 1 and name not in _GENERIC_METHOD_NAMES and not name.startswith("__")
        }
        self._namespaces: dict[str, dict[str, Target | tuple[str, str]]] = {}
        self._building: set[str] = set()
        self._mro: dict[str, list[str]] = {}

    @staticmethod
    def _build_aliases(modules: list[PyModule]) -> dict[str, str]:
        """Extra import names for modules outside regular packages (scripts, namespace dirs)."""
        counts: dict[str, list[str]] = defaultdict(list)
        for m in modules:
            if not m.loose:  # inside a regular package: its id is already the import name
                continue
            parts = m.path_name.split(".")
            for start in range(1, len(parts) - 1):
                alias = ".".join(parts[start:])
                if alias.split(".")[0] not in STDLIB:
                    counts[alias].append(m.id)
        return {alias: ids[0] for alias, ids in counts.items() if len(ids) == 1}

    # Modules and imports ---------------------------------------------------------

    def find_module(self, name: str, importer: PyModule | None = None) -> str | None:
        if not name:
            return None
        # A loose script (not inside a regular package) can import its neighbours by
        # bare name, because Python puts the script's directory first on sys.path.
        if importer is not None and importer.loose:
            prefix = importer.path_name.rpartition(".")[0]
            sibling = f"{prefix}.{name}" if prefix else name
            if sibling in self.by_path_name:
                return self.by_path_name[sibling]
        candidates = [name]
        if not self.root_is_package:
            candidates.append(self.by_path_name.get(name))
        candidates.append(self.aliases.get(name))
        for candidate in candidates:
            found = self.modules.get(candidate) if candidate else None
            # A loose script called json.py only shadows the stdlib for its neighbours.
            if found is None or (found.loose and name.split(".")[0] in STDLIB):
                continue
            return found.id
        return None

    def absolute_module(self, importer: PyModule, binding: ImportBinding) -> str | None:
        """The absolute dotted module an import refers to; None if the relative path escapes."""
        if binding.level == 0:
            return binding.module
        package = importer.id.split(".") if importer.is_package else importer.id.split(".")[:-1]
        up = binding.level - 1
        if up > len(package):
            return None
        base = ".".join(package[: len(package) - up])
        if base and binding.module:
            return f"{base}.{binding.module}"
        return base or binding.module

    def module_or_external(self, name: str, importer: PyModule) -> Target:
        found = self.find_module(name, importer)
        if found:
            return Target("module", found)
        return Target("external", name)

    def binding_target(
        self, importer: PyModule, binding: ImportBinding
    ) -> Target | tuple[str, str] | None:
        """What a single import binding makes its local name refer to.

        A ``(module id, name)`` tuple means "whatever that module exports as name",
        resolved lazily so re-exports through ``__init__.py`` are followed.
        """
        full = self.absolute_module(importer, binding)
        if full is None:
            return None
        if binding.name is None:  # import a.b.c  /  import a.b.c as x
            top = full.split(".")[0]
            return self.module_or_external(top if binding.local == top else full, importer)
        source = self.find_module(full, importer) if full else None
        if source is None and binding.level == 0:
            return Target("external", f"{full}.{binding.name}")
        if source is None:  # "from . import x" at the top of a tree of plain directories
            found = self.find_module(binding.name, importer)
            return Target("module", found) if found else None
        submodule = f"{source}.{binding.name}"
        if submodule in self.modules:
            return Target("module", submodule)
        return (source, binding.name)

    def import_dependency(self, importer: PyModule, binding: ImportBinding) -> str | None:
        """The graph node an import statement depends on (module id or ``ext:<top>``)."""
        full = self.absolute_module(importer, binding)
        if full is None:
            return None
        candidates = []
        if binding.name and binding.name != "*":
            candidates.append(f"{full}.{binding.name}" if full else binding.name)
        if full:
            parts = full.split(".")
            candidates += [".".join(parts[:i]) for i in range(len(parts), 0, -1)]
        for candidate in candidates:
            found = self.find_module(candidate, importer)
            if found:
                return found
        if binding.level > 0 or not full:
            return None
        return f"ext:{full.split('.')[0]}"

    def namespace(self, mod_id: str) -> dict[str, Target | tuple[str, str]]:
        """Module-level names: imports first, then definitions (which win)."""
        if mod_id in self._namespaces:
            return self._namespaces[mod_id]
        if mod_id in self._building:  # star-import cycle
            return {}
        self._building.add(mod_id)
        module = self.modules[mod_id]
        ns: dict[str, Target | tuple[str, str]] = {}
        for binding in module.info.imports:
            if binding.local is None:  # from x import *
                full = self.absolute_module(module, binding)
                source = self.find_module(full, module) if full else None
                if source:
                    for name in self.exported_names(source):
                        ns.setdefault(name, (source, name))
                continue
            target = self.binding_target(module, binding)
            if target is not None:
                ns[binding.local] = target
        for name in [*module.info.assigned_from_call, *module.info.annotated]:
            ns[name] = Target("variable", f"{mod_id}:{name}")
        for qual in module.info.children:
            ns[qual] = Target("symbol", f"{mod_id}:{qual}")
        self._building.discard(mod_id)
        self._namespaces[mod_id] = ns
        return ns

    def exported_names(self, mod_id: str) -> list[str]:
        module = self.modules[mod_id]
        if module.info.dunder_all is not None:
            return module.info.dunder_all
        return [n for n in self.namespace(mod_id) if not n.startswith("_")]

    def resolve_export(self, mod_id: str, name: str, depth: int = 0) -> Target | None:
        if depth > MAX_RESOLVE_DEPTH:
            return None
        entry = self.namespace(mod_id).get(name)
        if isinstance(entry, tuple):
            source, attr = entry
            return self.resolve_export(source, attr, depth + 1)
        if entry is not None:
            return entry
        submodule = f"{mod_id}.{name}"
        return Target("module", submodule) if submodule in self.modules else None

    # Names inside code -----------------------------------------------------------

    def scope_chain(self, ctx: Ctx) -> list[str]:
        """Qualnames visible from ``ctx``, innermost first, without enclosing class bodies."""
        chain, qual = [], ctx.qualname
        while qual:
            if qual == ctx.qualname or qual in ctx.module.info.functions:
                chain.append(qual)
            qual = qual.rpartition(".")[0]
        return chain

    def lookup(self, ctx: Ctx, name: str) -> Target | None:
        info = ctx.module.info
        for qual in self.scope_chain(ctx):
            scope = info.scope(qual)
            for child in scope.children:
                if child.rpartition(".")[2] == name:
                    return Target("symbol", f"{ctx.module.id}:{child}")
            for binding in reversed(scope.imports):
                if binding.local == name:
                    target = self.binding_target(ctx.module, binding)
                    if isinstance(target, tuple):
                        return self.resolve_export(*target)
                    return target
        entry = self.namespace(ctx.module.id).get(name)
        if isinstance(entry, tuple):
            return self.resolve_export(*entry)
        if entry is not None:
            return entry
        if name in BUILTINS:
            return Target("builtin", name)
        return None

    def member(self, target: Target, name: str) -> Target | None:
        if target.kind == "module":
            return self.resolve_export(target.ref, name)
        if target.kind == "external":
            return Target("external", f"{target.ref}.{name}")
        if self.is_class(target):
            found = self.find_member(target.ref, name)
            return Target("symbol", found) if found else None
        return None

    def resolve_parts(self, ctx: Ctx, parts: Parts) -> Target | None:
        target = self.lookup(ctx, parts[0])
        for part in parts[1:]:
            if target is None:
                return None
            target = self.member(target, part)
        return target

    # Classes and types -----------------------------------------------------------

    def is_class(self, target: Target | None) -> bool:
        return target is not None and target.kind == "symbol" and target.ref in self.classes

    def is_function(self, target: Target | None) -> bool:
        return target is not None and target.kind == "symbol" and target.ref in self.functions

    def class_ctx(self, class_id: str) -> Ctx:
        module, cls = self.classes[class_id]
        return Ctx(module, cls.qualname.rpartition(".")[0])

    def mro(self, class_id: str) -> list[str]:
        """Internal classes in method-lookup order (depth-first, left to right)."""
        if class_id in self._mro:
            return self._mro[class_id]
        self._mro[class_id] = [class_id]  # guards against inheritance cycles
        order = [class_id]
        for base in self.base_classes(class_id):
            for c in self.mro(base):
                if c not in order:
                    order.append(c)
        self._mro[class_id] = order
        return order

    def base_classes(self, class_id: str) -> list[str]:
        _, cls = self.classes[class_id]
        ctx = self.class_ctx(class_id)
        found = []
        for parts in cls.base_parts:
            target = self.resolve_parts(ctx, parts)
            if self.is_class(target):
                found.append(target.ref)
        return found

    def find_member(self, class_id: str, name: str, skip_self: bool = False) -> str | None:
        order = self.mro(class_id)
        for c in order[1:] if skip_self else order:
            candidate = f"{c}.{name}"
            if candidate in self.functions or candidate in self.classes:
                return candidate
        return None

    # A "type" below is a Target: an internal class (kind "symbol"), an external class
    # or factory result (kind "external"), or a builtin such as dict (kind "builtin").

    def as_type(self, target: Target | None) -> Target | None:
        if target is None:
            return None
        if target.kind in ("external", "builtin") or self.is_class(target):
            return target
        return None

    def type_from_annotation(self, ctx: Ctx, parts: Parts | None) -> Target | None:
        return self.as_type(self.resolve_parts(ctx, parts)) if parts else None

    def type_from_call(self, ctx: Ctx, parts: Parts) -> Target | None:
        """The type of the object a call returns: a constructor or an annotated factory."""
        own_class = self.classmethod_owner(ctx)
        if own_class and parts == (ctx.module.info.functions[ctx.qualname].self_name,):
            return Target("symbol", own_class)  # cls(...) inside a classmethod
        target = self.resolve_parts(ctx, parts)
        if self.is_function(target):
            module, func = self.functions[target.ref]
            return self.type_from_annotation(Ctx(module, func.qualname), func.returns)
        return self.as_type(target)

    def type_of_name(self, ctx: Ctx, name: str, include_module: bool = True) -> Target | None:
        info = ctx.module.info
        for qual in [*self.scope_chain(ctx), *([""] if include_module else [])]:
            scope = info.scope(qual)
            if name in scope.annotated:
                return self.type_from_annotation(Ctx(ctx.module, qual), scope.annotated[name])
            if name in scope.assigned_from_call:
                return self.type_from_call(Ctx(ctx.module, qual), scope.assigned_from_call[name])
        return None

    def type_of_attr(self, class_id: str, attr: str) -> Target | None:
        for c in self.mro(class_id):
            _, cls = self.classes[c]
            ctx = self.class_ctx(c)
            if attr in cls.attr_types:
                return self.type_from_annotation(ctx, cls.attr_types[attr])
            if attr in cls.attr_from_param:
                return self.type_from_annotation(ctx, cls.attr_from_param[attr])
            if attr in cls.attr_from_call:
                return self.type_from_call(ctx, cls.attr_from_call[attr])
            prop = self.functions.get(f"{c}.{attr}")
            if prop and "property" in prop[1].decorators:
                return self.type_from_annotation(Ctx(prop[0], prop[1].qualname), prop[1].returns)
        return None

    def external_base(self, class_id: str) -> Target | None:
        """The first external base class anywhere in the internal inheritance chain."""
        for c in self.mro(class_id):
            _, cls = self.classes[c]
            for parts in cls.base_parts:
                target = self.resolve_parts(self.class_ctx(c), parts)
                if target and target.kind == "external":
                    return target
        return None

    def classmethod_owner(self, ctx: Ctx) -> str | None:
        func = ctx.module.info.functions.get(ctx.qualname)
        if func and func.self_name and any(d == "classmethod" for d in func.decorators):
            return self.enclosing_class(ctx)
        return None

    def enclosing_class(self, ctx: Ctx) -> str | None:
        parent = ctx.qualname.rpartition(".")[0]
        if parent in ctx.module.info.classes:
            return f"{ctx.module.id}:{parent}"
        return None

    # Calls -------------------------------------------------------------------------

    def resolve_call(self, ctx: Ctx, parts: Parts) -> tuple[str, str] | Target | None:
        """Return ``(symbol id, confidence)``, an external/builtin Target, or None."""
        head, rest = parts[0], parts[1:]
        func = ctx.module.info.functions.get(ctx.qualname)
        own_class = self.enclosing_class(ctx) if func else None

        if own_class and head == "super()" and len(rest) == 1:
            found = self.find_member(own_class, rest[0], skip_self=True)
            if found:
                return (found, Confidence.RESOLVED)
            external = self.external_base(own_class)
            if external:
                return Target("external", f"{external.ref}.{rest[0]}")
            return Target("builtin", f"object.{rest[0]}")
        if own_class and func and head == func.self_name:
            if rest:
                return self.via_type(Target("symbol", own_class), rest, Confidence.RESOLVED, parts)
            if self.classmethod_owner(ctx):
                return (own_class, Confidence.RESOLVED)  # cls(...) builds an instance

        local_type = self.type_of_name(ctx, head, include_module=False)
        if local_type is not None and rest:
            return self.via_type(local_type, rest, Confidence.INFERRED, parts)

        target = self.lookup(ctx, head)
        if target is None:
            return self.guess(parts)
        for i, part in enumerate(rest):
            if target.kind == "variable":  # a module-level object: continue from its type
                return self.via_type(
                    self.variable_type(target), rest[i:], Confidence.INFERRED, parts
                )
            if target.kind == "builtin":
                return target
            target = self.member(target, part)
            if target is None:
                return self.guess(parts)
        if self.is_function(target) or self.is_class(target):
            return (target.ref, Confidence.RESOLVED)
        return target if target.kind in ("external", "builtin") else None

    def variable_type(self, target: Target) -> Target | None:
        mod_id, _, name = target.ref.partition(":")
        return self.type_of_name(Ctx(self.modules[mod_id], ""), name)

    def class_attr_alias(self, class_id: str, attr: str) -> str | None:
        """``session_class = SecureCookieSession`` -> the class or function it names."""
        for c in self.mro(class_id):
            parts = self.classes[c][1].attr_alias.get(attr)
            if parts:
                target = self.resolve_parts(self.class_ctx(c), parts)
                if self.is_function(target) or self.is_class(target):
                    return target.ref
        return None

    def via_type(
        self, start: Target | None, rest: Parts, confidence: str, parts: Parts
    ) -> tuple[str, str] | Target | None:
        """Follow ``rest`` (attributes, then a method) from an object of type ``start``."""
        current = start
        for i, attr in enumerate(rest):
            if current is None:
                return self.guess(parts)
            if current.kind == "external":
                return Target("external", ".".join([current.ref, *rest[i:]]))
            if current.kind == "builtin":
                return current
            if i == len(rest) - 1:
                break
            current = self.type_of_attr(current.ref, attr)
            confidence = Confidence.INFERRED
        assert current is not None
        found = self.find_member(current.ref, rest[-1])
        if found:
            return (found, confidence)
        aliased = self.class_attr_alias(current.ref, rest[-1])
        if aliased:
            return (aliased, Confidence.INFERRED)
        external = self.external_base(current.ref)
        if external:
            return Target("external", f"{external.ref}.{rest[-1]}")
        return self.guess(parts)

    def guess(self, parts: Parts) -> tuple[str, str] | None:
        if len(parts) >= 2 and parts[-1] in self.unique_methods:
            return (self.unique_methods[parts[-1]], Confidence.GUESSED)
        return None

    # Graph construction ----------------------------------------------------------

    def build(self, scripts: dict[str, str]) -> None:
        for module in sorted(self.modules.values(), key=lambda m: m.id):
            self._add_module(module)
        for module in sorted(self.modules.values(), key=lambda m: m.id):
            self._add_imports(module)
            self._add_definitions_links(module)
        self._mark_scripts(scripts)

    def _add_module(self, module: PyModule) -> None:
        info = module.info
        attrs: dict = {"is_package": True} if module.is_package else {}
        if info.code_hash:
            attrs["hash"] = info.code_hash
        if info.error:
            attrs["parse_error"] = info.error
        if info.has_main_guard:
            attrs["entry_point"] = "main-guard"
        if module.path.endswith("/__main__.py") or module.path == "__main__.py":
            attrs["entry_point"] = "main-module"
        self.graph.add_node(
            Node(
                id=module.id,
                kind=NodeKind.MODULE,
                name=posixpath.basename(module.path),
                path=module.path,
                loc=info.loc,
                docstring=info.docstring,
                attrs=attrs,
            )
        )
        self.graph.add_edge(
            Edge(f"dir:{posixpath.dirname(module.path) or '.'}", module.id, EdgeKind.CONTAINS)
        )
        for qual, cls in info.classes.items():
            cid = f"{module.id}:{qual}"
            attrs = {"hash": cls.code_hash}
            if cls.bases:
                attrs["bases"] = cls.bases
            if cls.decorators:
                attrs["decorators"] = cls.decorators
            self.graph.add_node(
                Node(
                    id=cid,
                    kind=NodeKind.CLASS,
                    name=cls.name,
                    path=module.path,
                    line=cls.line,
                    end_line=cls.end_line,
                    loc=cls.end_line - cls.line + 1,
                    docstring=cls.docstring,
                    attrs=attrs,
                )
            )
        for qual, func in info.functions.items():
            attrs = {"hash": func.code_hash}
            if func.shape_hash:
                attrs["shape"] = func.shape_hash
            if func.is_async:
                attrs["async"] = True
            if func.decorators:
                attrs["decorators"] = func.decorators
            entry = _entry_point_kind(func)
            if entry:
                attrs["entry_point"] = entry
            self.graph.add_node(
                Node(
                    id=f"{module.id}:{qual}",
                    kind=NodeKind.METHOD if func.is_method else NodeKind.FUNCTION,
                    name=func.name,
                    path=module.path,
                    line=func.line,
                    end_line=func.end_line,
                    loc=func.end_line - func.line + 1,
                    docstring=func.docstring,
                    signature=func.signature,
                    attrs=attrs,
                )
            )
        for qual in ["", *info.classes, *info.functions]:
            parent = module.id if qual == "" else f"{module.id}:{qual}"
            for child in dict.fromkeys(info.scope(qual).children):
                self.graph.add_edge(Edge(parent, f"{module.id}:{child}", EdgeKind.CONTAINS))

    def _add_imports(self, module: PyModule) -> None:
        bindings = [*module.info.imports]
        for scope in [*module.info.functions.values(), *module.info.classes.values()]:
            bindings += scope.imports
        grouped: dict[str, list[ImportBinding]] = defaultdict(list)
        unresolved = []
        for binding in bindings:
            if binding.module == "__future__":
                continue
            target = self.import_dependency(module, binding)
            if target is None:
                unresolved.append("." * binding.level + binding.module)
            elif target != module.id:
                grouped[target].append(binding)
        if unresolved:
            self.graph.nodes[module.id].attrs["unresolved_imports"] = sorted(set(unresolved))
        for target, group in grouped.items():
            if target.startswith("ext:"):
                top = target[len("ext:") :]
                self.graph.add_node(
                    Node(
                        id=target, kind=NodeKind.EXTERNAL, name=top, attrs={"stdlib": top in STDLIB}
                    )
                )
            attrs: dict = {}
            names = sorted({b.name for b in group if b.name})
            if names:
                attrs["names"] = names
            if all(b.type_only for b in group):
                attrs["type_only"] = True
            line = min(b.line for b in group)
            self.graph.add_edge(Edge(module.id, target, EdgeKind.IMPORTS, line=line, attrs=attrs))

    def _add_definitions_links(self, module: PyModule) -> None:
        info = module.info
        for qual, cls in info.classes.items():
            cid = f"{module.id}:{qual}"
            for base in self.base_classes(cid):
                self.graph.add_edge(
                    Edge(cid, base, EdgeKind.INHERITS, Confidence.RESOLVED, cls.line)
                )
        for qual, scope in [*info.classes.items(), *info.functions.items()]:
            parent_ctx = Ctx(module, qual.rpartition(".")[0])
            for parts in scope.decorator_parts:
                target = self.resolve_parts(parent_ctx, parts)
                if self.is_function(target):
                    self.graph.add_edge(
                        Edge(
                            target.ref,
                            f"{module.id}:{qual}",
                            EdgeKind.DECORATES,
                            Confidence.RESOLVED,
                            scope.line,
                        )
                    )
        for qual in ["", *info.classes, *info.functions]:
            self._add_calls(Ctx(module, qual))

    def _add_calls(self, ctx: Ctx) -> None:
        scope = ctx.module.info.scope(ctx.qualname)
        caller = ctx.module.id if ctx.qualname == "" else f"{ctx.module.id}:{ctx.qualname}"
        external: set[str] = set()
        unresolved = 0
        unresolved_names: set[str] = set()
        for call in scope.calls:
            result = self.resolve_call(ctx, call.parts)
            if isinstance(result, tuple):
                target, confidence = result
                if target != caller:
                    self.graph.add_edge(Edge(caller, target, EdgeKind.CALLS, confidence, call.line))
            elif isinstance(result, Target):
                if result.kind == "external":
                    external.add(result.ref)
            else:
                unresolved += 1
                unresolved_names.add(call.parts[-1])
        node = self.graph.nodes[caller]
        if external:
            node.attrs["external_calls"] = sorted(external)[:MAX_EXTERNAL_CALLS]
        if unresolved:
            node.attrs["unresolved_calls"] = unresolved
            # Kept so a diff can tell "the call was removed" from "the call is still
            # there but could not be linked this time".
            node.attrs["unresolved_names"] = sorted(unresolved_names)[:MAX_UNRESOLVED_NAMES]

    def _mark_scripts(self, scripts: dict[str, str]) -> None:
        """Mark ``[project.scripts]`` targets such as ``shop.cli:main`` as entry points."""
        for script, target in scripts.items():
            mod_name, _, attr = target.partition(":")
            mod_id = self.find_module(mod_name.strip())
            if mod_id is None:
                continue
            node = self.graph.nodes.get(f"{mod_id}:{attr.strip()}") or self.graph.nodes[mod_id]
            node.attrs["entry_point"] = f"script:{script}"


def _entry_point_kind(func: FunctionInfo) -> str | None:
    for parts in func.decorator_parts:
        last = parts[-1]
        if len(parts) >= 2 and last in _HTTP_DECORATORS:
            return "http"
        if (len(parts) >= 2 and last in _CLI_DECORATORS) or parts == ("command",):
            return "cli"
        if last in ("task", "shared_task"):
            return "task"
    return None
