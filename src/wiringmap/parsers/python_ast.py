"""Extract facts from a single Python file.

This step looks at one file in isolation: what it defines, what it imports, what it
calls and which types it hints at. Turning names into links between files happens
later in :mod:`wiringmap.parsers.python_resolve`, once every file has been extracted.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from dataclasses import dataclass, field

MAX_DOCSTRING_CHARS = 1000

Parts = tuple[str, ...]  # a dotted name split up, e.g. ("self", "repo", "save")


@dataclass
class ImportBinding:
    """One name bound by an import statement."""

    local: str | None  # the bound name; None for "from x import *"
    module: str  # module path as written, without leading dots ("" for "from . import x")
    level: int  # number of leading dots for relative imports
    name: str | None  # imported attribute for "from" imports, None for plain "import"
    line: int
    type_only: bool = False  # inside an "if TYPE_CHECKING:" block


@dataclass
class CallSite:
    parts: Parts
    line: int


@dataclass
class Scope:
    """Facts shared by modules, classes and functions."""

    qualname: str  # "" for the module itself
    calls: list[CallSite] = field(default_factory=list)
    imports: list[ImportBinding] = field(default_factory=list)
    # Names assigned from a call, e.g. ``svc = OrderService(repo)`` -> {"svc": ("OrderService",)}
    assigned_from_call: dict[str, Parts] = field(default_factory=dict)
    # Names with a type annotation, e.g. ``svc: OrderService`` -> {"svc": ("OrderService",)}
    annotated: dict[str, Parts] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)  # qualnames of directly nested defs


@dataclass
class FunctionInfo(Scope):
    name: str = ""
    line: int = 0
    end_line: int = 0
    docstring: str | None = None
    signature: str = ""
    is_async: bool = False
    is_method: bool = False
    decorators: list[str] = field(default_factory=list)
    decorator_parts: list[Parts] = field(default_factory=list)
    self_name: str | None = None  # first parameter of a method ("self" / "cls")
    returns: Parts | None = None
    code_hash: str = ""  # changes when the code changes; not when it moves or its docstring does
    shape_hash: str | None = None  # same for the same logic under different names; None if tiny


@dataclass
class ClassInfo(Scope):
    name: str = ""
    line: int = 0
    end_line: int = 0
    docstring: str | None = None
    bases: list[str] = field(default_factory=list)  # as written
    base_parts: list[Parts] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    decorator_parts: list[Parts] = field(default_factory=list)
    # Instance attribute types from class annotations, ``self.x: T`` or ``self.x = T(...)``.
    attr_types: dict[str, Parts] = field(default_factory=dict)
    # ``self.x = param`` where ``param`` is annotated, resolved once all scopes are known.
    attr_from_param: dict[str, Parts] = field(default_factory=dict)
    # ``self.x = T(...)``: the call target, which may be a class or a factory function.
    attr_from_call: dict[str, Parts] = field(default_factory=dict)
    # ``session_class = SecureCookieSession``: a class attribute naming another class/function.
    attr_alias: dict[str, Parts] = field(default_factory=dict)
    code_hash: str = ""


@dataclass
class ModuleInfo(Scope):
    path: str = ""
    docstring: str | None = None
    loc: int = 0
    has_main_guard: bool = False
    dunder_all: list[str] | None = None
    functions: dict[str, FunctionInfo] = field(default_factory=dict)  # by qualname
    classes: dict[str, ClassInfo] = field(default_factory=dict)  # by qualname
    error: str | None = None
    code_hash: str = ""  # of the file's text, line endings normalised

    def scope(self, qualname: str) -> Scope:
        if qualname == "":
            return self
        return self.functions.get(qualname) or self.classes[qualname]


def read_source(data: bytes) -> str:
    """Decode Python source honouring a PEP 263 coding cookie and a BOM."""
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def extract_module(path: str, source: str) -> ModuleInfo:
    """Extract the facts of one Python file; syntax errors are recorded, not raised."""
    info = ModuleInfo(qualname="", path=path)
    info.loc = sum(1 for line in source.splitlines() if line.strip())
    info.code_hash = _digest("\n".join(source.splitlines()))
    try:
        tree = _parse(source, path)
        info.docstring = _docstring(tree)
        _Extractor(info).visit_body(tree.body)
    except (SyntaxError, ValueError, RecursionError) as exc:
        # ValueError: null bytes in the source. RecursionError: pathologically deep nesting.
        info.error = f"{type(exc).__name__}: {exc}"
    return info


# Python 3.14 (PEP 758) allows "except A, B:" without parentheses. Older interpreters
# reject it, so on those we add the parentheses on that line and try again.
_BARE_EXCEPT_TUPLE = re.compile(r"^(\s*except\*?\s+)([^:(\n][^:\n]*,[^:\n]*?)(\s+as\s+\w+)?\s*:")
_MAX_SYNTAX_RETRIES = 50


def _parse(source: str, path: str) -> ast.Module:
    for _ in range(_MAX_SYNTAX_RETRIES):
        try:
            return ast.parse(source, filename=path)
        except SyntaxError as exc:
            if "must be parenthesized" not in str(exc.msg) or not exc.lineno:
                raise
            lines = source.split("\n")
            fixed = _BARE_EXCEPT_TUPLE.sub(r"\1(\2)\3:", lines[exc.lineno - 1], count=1)
            if fixed == lines[exc.lineno - 1]:
                raise
            lines[exc.lineno - 1] = fixed
            source = "\n".join(lines)
    return ast.parse(source, filename=path)


def dotted_parts(node: ast.AST) -> Parts | None:
    """``a.b.c`` -> ("a", "b", "c"); ``super().x`` -> ("super()", "x"); anything else -> None."""
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        base = dotted_parts(node.value)
        return None if base is None else (*base, node.attr)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super":
        return ("super()",)
    return None


def annotation_parts(node: ast.AST | None) -> Parts | None:
    """The class an annotation points at: ``T``, ``"T"``, ``Optional[T]`` and ``T | None``."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return annotation_parts(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        sides = [s for s in (node.left, node.right) if not _is_none(s)]
        return annotation_parts(sides[0]) if len(sides) == 1 else None
    if isinstance(node, ast.Subscript):
        wrapper = dotted_parts(node.value)
        if wrapper and wrapper[-1] in ("Optional", "Annotated", "ClassVar", "Final"):
            inner = node.slice
            if isinstance(inner, ast.Tuple):  # Annotated[T, ...]
                inner = inner.elts[0]
            return annotation_parts(inner)
        if wrapper and wrapper[-1] == "Union":
            return None
        return wrapper  # a generic such as list[T] or ContextVar[T] is still a list / ContextVar
    return dotted_parts(node)


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _docstring(node: ast.AST) -> str | None:
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    if not doc:
        return None
    return doc if len(doc) <= MAX_DOCSTRING_CHARS else doc[:MAX_DOCSTRING_CHARS] + "…"


def _is_type_checking(test: ast.expr) -> bool:
    parts = dotted_parts(test)
    return parts is not None and parts[-1] == "TYPE_CHECKING"


def _is_main_guard(test: ast.expr) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.comparators) == 1):
        return False
    sides = [test.left, test.comparators[0]]
    names = {s.id for s in sides if isinstance(s, ast.Name)}
    values = {s.value for s in sides if isinstance(s, ast.Constant)}
    return names == {"__name__"} and values == {"__main__"}


class _Extractor(ast.NodeVisitor):
    def __init__(self, module: ModuleInfo) -> None:
        self.module = module
        self.stack: list[Scope] = [module]
        self.type_checking = 0

    @property
    def scope(self) -> Scope:
        return self.stack[-1]

    def visit_body(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            self.visit(stmt)

    def _qualname(self, name: str) -> str:
        return f"{self.scope.qualname}.{name}" if self.scope.qualname else name

    # Definitions -----------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.scope
        is_method = isinstance(parent, ClassInfo)
        decorators = [ast.unparse(d) for d in node.decorator_list]
        is_static = any(d.split("(")[0].endswith("staticmethod") for d in decorators)
        positional = [*node.args.posonlyargs, *node.args.args]
        func = FunctionInfo(
            qualname=self._qualname(node.name),
            name=node.name,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            docstring=_docstring(node),
            signature=_signature(node),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            is_method=is_method,
            decorators=decorators,
            decorator_parts=[p for p in map(_decorator_parts, node.decorator_list) if p],
            self_name=positional[0].arg if is_method and positional and not is_static else None,
            returns=annotation_parts(node.returns),
        )
        for arg in [*positional, *node.args.kwonlyargs]:
            hinted = annotation_parts(arg.annotation)
            if hinted:
                func.annotated[arg.arg] = hinted
        if node.args.vararg:
            func.annotated[node.args.vararg.arg] = ("tuple",)
        if node.args.kwarg:
            func.annotated[node.args.kwarg.arg] = ("dict",)
        # Decorators and defaults run in the enclosing scope, not inside the function.
        for expr in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if expr is not None:
                self.visit(expr)
        func.code_hash = _digest(_dump_def(node, [node.args, node.returns, *node.decorator_list]))
        func.shape_hash = _shape_hash(node)
        self.module.functions[func.qualname] = func
        parent.children.append(func.qualname)
        self.stack.append(func)
        self.visit_body(node.body)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent = self.scope
        cls = ClassInfo(
            qualname=self._qualname(node.name),
            name=node.name,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            docstring=_docstring(node),
            bases=[ast.unparse(b) for b in node.bases],
            base_parts=[p for p in map(dotted_parts, node.bases) if p],
            decorators=[ast.unparse(d) for d in node.decorator_list],
            decorator_parts=[p for p in map(_decorator_parts, node.decorator_list) if p],
        )
        for expr in [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]:
            self.visit(expr)
        cls.code_hash = _digest(
            _dump_def(node, [*node.bases, *node.keywords, *node.decorator_list])
        )
        self.module.classes[cls.qualname] = cls
        parent.children.append(cls.qualname)
        self.stack.append(cls)
        self.visit_body(node.body)
        self.stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.body)  # calls inside a lambda belong to the enclosing scope

    # Imports --------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self._bind(local, alias.name, 0, None, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = None if alias.name == "*" else alias.asname or alias.name
            self._bind(local, node.module or "", node.level, alias.name, node.lineno)

    def _bind(self, local: str | None, module: str, level: int, name: str | None, line: int):
        binding = ImportBinding(local, module, level, name, line, self.type_checking > 0)
        self.scope.imports.append(binding)

    # Statements that carry type information -------------------------------------

    def visit_If(self, node: ast.If) -> None:
        if self.scope is self.module and _is_main_guard(node.test):
            self.module.has_main_guard = True
        if _is_type_checking(node.test):
            self.type_checking += 1
            self.visit_body(node.body)
            self.type_checking -= 1
            self.visit_body(node.orelse)
            return
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_assignment(target, node.value, None)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_assignment(node.target, node.value, node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._record_assignment(item.optional_vars, item.context_expr, None)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def _record_assignment(
        self, target: ast.expr, value: ast.expr | None, annotation: ast.expr | None
    ) -> None:
        hinted = annotation_parts(annotation) or _literal_type(value)
        called = dotted_parts(value.func) if isinstance(value, ast.Call) else None
        scope = self.scope

        if isinstance(target, ast.Name):
            if hinted:
                scope.annotated[target.id] = hinted
            elif called:
                scope.assigned_from_call[target.id] = called
            if isinstance(scope, ModuleInfo) and target.id == "__all__":
                self.module.dunder_all = _string_list(value)
            if isinstance(scope, ClassInfo):  # class attribute or dataclass-style field
                if hinted:
                    scope.attr_types.setdefault(target.id, hinted)
                elif called:
                    scope.attr_from_call.setdefault(target.id, called)
                elif value is not None and (named := dotted_parts(value)):
                    scope.attr_alias.setdefault(target.id, named)
            return

        # self.<attr> = ... inside a method: remember the attribute's type on the class.
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and isinstance(scope, FunctionInfo)
            and scope.self_name == target.value.id
            and len(self.stack) >= 2
            and isinstance(self.stack[-2], ClassInfo)
        ):
            return
        cls = self.stack[-2]
        attr = target.attr
        if hinted:
            cls.attr_types.setdefault(attr, hinted)
        elif called:
            cls.attr_from_call.setdefault(attr, called)
        elif isinstance(value, ast.Name) and value.id in scope.annotated:
            cls.attr_from_param.setdefault(attr, scope.annotated[value.id])

    # Calls ------------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        parts = dotted_parts(node.func)
        if parts and parts not in (("super",), ("super()",)):  # super() alone is plumbing
            self.scope.calls.append(CallSite(parts, node.lineno))
        self.generic_visit(node)


_LITERAL_TYPES: dict[type, Parts] = {
    ast.List: ("list",),
    ast.ListComp: ("list",),
    ast.Dict: ("dict",),
    ast.DictComp: ("dict",),
    ast.Set: ("set",),
    ast.SetComp: ("set",),
    ast.Tuple: ("tuple",),
    ast.JoinedStr: ("str",),
}


def _literal_type(node: ast.expr | None) -> Parts | None:
    """The builtin type of a literal value: ``[]`` -> list, ``"x"`` -> str."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes, int, float)):
        return (type(node.value).__name__,)
    return _LITERAL_TYPES.get(type(node))


def _decorator_parts(node: ast.expr) -> Parts | None:
    return dotted_parts(node.func if isinstance(node, ast.Call) else node)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    text = f"({ast.unparse(node.args)})"
    if node.returns is not None:
        text += f" -> {ast.unparse(node.returns)}"
    return text


def _string_list(node: ast.expr | None) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return [e.value for e in node.elts]  # type: ignore[attr-defined]
    return None


# Fingerprints -----------------------------------------------------------------------

# Functions with fewer statements than this get no shape: "return self.x" would match
# hundreds of unrelated getters.
MIN_SHAPE_STATEMENTS = 3


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _body_without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _dump_def(node, header: list) -> str:
    """The definition as text, without positions or docstring, so moving it changes nothing."""
    parts = [ast.dump(h, include_attributes=False) for h in header if h is not None]
    parts += [ast.dump(stmt, include_attributes=False) for stmt in _body_without_docstring(node)]
    return "\n".join(parts)


def _shape_hash(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The body's structure with local names blanked out, to spot the same logic renamed.

    Attribute names, literal values and the names of called functions are kept:
    ``price.quantize(...)``, ``round(x, 2)`` and ``validate(x)`` say what the code does,
    while variable names do not.
    """
    body = _body_without_docstring(node)
    nodes = [n for stmt in body for n in ast.walk(stmt)]
    if sum(isinstance(n, ast.stmt) for n in nodes) < MIN_SHAPE_STATEMENTS:
        return None
    callees = {id(n.func) for n in nodes if isinstance(n, ast.Call)}
    tokens = [str(len(node.args.args))]
    for n in nodes:
        token = type(n).__name__
        if isinstance(n, ast.Attribute):
            token += "." + n.attr
        elif isinstance(n, ast.Constant):
            token += "=" + repr(n.value)[:40]
        elif isinstance(n, ast.Name) and id(n) in callees:
            token += ":" + n.id
        tokens.append(token)
    return _digest(" ".join(tokens))
