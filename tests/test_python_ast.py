import ast
import sys
import textwrap

import pytest

from wiringmap.parsers.python_ast import annotation_parts, extract_module, read_source


def extract(source: str):
    return extract_module("m.py", textwrap.dedent(source))


def annotation(text: str):
    return annotation_parts(ast.parse(text, mode="eval").body)


def test_definitions_signatures_and_docstrings():
    info = extract(
        '''
        """Module doc."""

        class A(Base, metaclass=Meta):
            """Class doc."""

            @staticmethod
            def s(x: int = 1, *args, **kw) -> str:
                """Static doc."""

            async def m(self):
                def inner():
                    pass
        '''
    )
    assert info.docstring == "Module doc."
    assert info.classes["A"].bases == ["Base"]
    assert info.classes["A"].docstring == "Class doc."
    s = info.functions["A.s"]
    assert s.is_method and s.self_name is None
    assert s.signature == "(x: int=1, *args, **kw) -> str"
    assert s.annotated == {"x": ("int",), "args": ("tuple",), "kw": ("dict",)}
    m = info.functions["A.m"]
    assert m.is_async and m.self_name == "self"
    assert m.children == ["A.m.inner"]
    assert not info.functions["A.m.inner"].is_method


def test_calls_are_attributed_to_the_innermost_scope():
    info = extract(
        """
        setup()

        @register("x")
        def f(cb=default_cb()):
            helper()
            obj.method().chain()
            items = [transform(i) for i in data]
            super().save()
            fn = lambda: inside_lambda()

        if __name__ == "__main__":
            f()
        """
    )
    module_calls = {c.parts for c in info.calls}
    assert module_calls == {("setup",), ("register",), ("default_cb",), ("f",)}
    f_calls = {c.parts for c in info.functions["f"].calls}
    assert f_calls == {
        ("helper",),
        ("obj", "method"),
        ("transform",),
        ("super()", "save"),
        ("inside_lambda",),
    }
    assert info.has_main_guard


def test_imports_and_type_checking():
    info = extract(
        """
        import os.path
        import numpy as np
        from . import sibling
        from ..pkg.mod import Thing as T, other
        from x import *
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from heavy import Model

        def f():
            import json
        """
    )
    rows = [(b.local, b.module, b.level, b.name, b.type_only) for b in info.imports]
    assert rows == [
        ("os", "os.path", 0, None, False),
        ("np", "numpy", 0, None, False),
        ("sibling", "", 1, "sibling", False),
        ("T", "pkg.mod", 2, "Thing", False),
        ("other", "pkg.mod", 2, "other", False),
        (None, "x", 0, "*", False),
        ("TYPE_CHECKING", "typing", 0, "TYPE_CHECKING", False),
        ("Model", "heavy", 0, "Model", True),
    ]
    assert [b.module for b in info.functions["f"].imports] == ["json"]


def test_assignment_types():
    info = extract(
        """
        registry = Registry()
        names: list[str] = []

        class Service:
            repo_class = Repo
            cache: Cache

            def __init__(self, repo: Repo, client):
                self.repo = repo
                self.client = Client()
                self.items = []
                self.hinted: Thing = make()
        """
    )
    assert info.assigned_from_call == {"registry": ("Registry",)}
    assert info.annotated["names"] == ("list",)
    service = info.classes["Service"]
    assert service.attr_alias == {"repo_class": ("Repo",)}
    assert service.attr_types == {"cache": ("Cache",), "items": ("list",), "hinted": ("Thing",)}
    assert service.attr_from_param == {"repo": ("Repo",)}
    assert service.attr_from_call == {"client": ("Client",)}


def test_annotation_parts():
    assert annotation("Order") == ("Order",)
    assert annotation("models.Order") == ("models", "Order")
    assert annotation("'Order'") == ("Order",)
    assert annotation("Optional[Order]") == ("Order",)
    assert annotation("Order | None") == ("Order",)
    assert annotation("Annotated[Order, 'meta']") == ("Order",)
    assert annotation("list[Order]") == ("list",)
    assert annotation("Union[A, B]") is None
    assert annotation("A | B") is None


def test_syntax_error_is_recorded():
    info = extract("def broken(:\n")
    assert info.error and info.error.startswith("SyntaxError")
    assert info.functions == {}


def test_read_source_honours_coding_cookie():
    data = "# -*- coding: latin-1 -*-\nname = 'café'\n".encode("latin-1")
    assert "café" in read_source(data)


def test_python_314_unparenthesized_except_is_accepted():
    info = extract(
        """
        def load():
            try:
                fetch()
            except ValueError, KeyError:
                retry()
        """
    )
    assert info.error is None
    assert {c.parts for c in info.functions["load"].calls} == {("fetch",), ("retry",)}


@pytest.mark.skipif(sys.version_info < (3, 11), reason="except* needs Python 3.11")
def test_python_314_unparenthesized_except_star_is_accepted():
    info = extract(
        """
        def load():
            try:
                fetch()
            except* OSError, TimeoutError:
                give_up()
        """
    )
    assert info.error is None
    assert {c.parts for c in info.functions["load"].calls} == {("fetch",), ("give_up",)}


# Fingerprints --------------------------------------------------------------------------

PRICE = """
def total(items, tax):
    \"\"\"Sum the items.\"\"\"
    subtotal = sum(i.price for i in items)
    if subtotal > 100:
        subtotal = subtotal * 0.9
    return round(subtotal * (1 + tax), 2)
"""


def fn(source: str, name: str):
    return extract(source).functions[name]


def test_moving_a_function_or_editing_its_docstring_keeps_its_hash():
    moved = "\n\n\n# a comment\n" + PRICE.replace("Sum the items.", "Add everything up.")
    assert fn(PRICE, "total").code_hash == fn(moved, "total").code_hash


def test_editing_code_changes_the_hash():
    edited = PRICE.replace("0.9", "0.8")
    assert fn(PRICE, "total").code_hash != fn(edited, "total").code_hash


def test_renamed_copy_has_the_same_shape_but_not_the_same_hash():
    copy = (
        PRICE.replace("def total", "def compute_sum")
        .replace("subtotal", "acc")
        .replace("items", "rows")
        .replace("tax", "rate")
    )
    a, b = fn(PRICE, "total"), fn(copy, "compute_sum")
    assert a.shape_hash is not None
    assert a.shape_hash == b.shape_hash
    assert a.code_hash != b.code_hash


def test_calling_a_different_function_is_a_different_shape():
    other = PRICE.replace("round(", "truncate(")
    assert fn(PRICE, "total").shape_hash != fn(other, "total").shape_hash


def test_tiny_functions_get_no_shape():
    assert fn("def get(self):\n    return self.x\n", "get").shape_hash is None


def test_class_hash_follows_its_methods():
    before = "class A:\n    def f(self):\n        return 1\n"
    after = before.replace("return 1", "return 2")
    assert extract(before).classes["A"].code_hash != extract(after).classes["A"].code_hash


def test_file_hash_ignores_line_endings():
    assert extract("x = 1\ny = 2\n").code_hash == extract("x = 1\r\ny = 2\r\n").code_hash
    assert extract("x = 1\n").code_hash != extract("x = 2\n").code_hash


def test_broken_file_still_gets_a_file_hash():
    info = extract("def broken(:\n")
    assert info.error and info.code_hash
