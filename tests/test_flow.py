import json

import pytest
from typer.testing import CliRunner

from wiringmap.cli import app
from wiringmap.flow import FlowError, build_flow, find, render_text

runner = CliRunner()

APP = {
    "shop/__init__.py": "",
    "shop/db.py": "def save(x):\n    return x\n\ndef load(x):\n    return x\n",
    "shop/cart.py": (
        "from shop.db import load, save\n\n"
        "def total(items):\n    return sum(items)\n\n"
        "def checkout(items):\n"
        "    cart = load(items)\n"
        "    price = total(cart)\n"
        "    save(price)\n"
        "    return price\n"
    ),
    "shop/api.py": (
        "from shop.cart import checkout\n\n"
        "@app.post('/buy')\ndef buy(items):\n    return checkout(items)\n"
    ),
}


def names(step):
    return [c.name for c in step.children]


def test_down_lists_calls_in_the_order_they_happen(graph_of):
    g = graph_of(APP)
    tree = build_flow(g, find(g, "checkout"))
    assert names(tree) == ["load", "total", "save"]
    assert [c.call_line for c in tree.children] == [7, 8, 9]


def test_up_lists_callers_and_marks_entry_points(graph_of):
    g = graph_of(APP)
    tree = build_flow(g, find(g, "save"), direction="up", depth=3)
    assert names(tree) == ["checkout"]
    assert names(tree.children[0]) == ["buy"]
    assert tree.children[0].children[0].entry_point == "http"


def test_depth_limit_counts_what_is_left(graph_of):
    g = graph_of(APP)
    tree = build_flow(g, find(g, "buy"), depth=1)
    assert names(tree) == ["checkout"]
    assert tree.children[0].more == 3  # load, total, save not expanded


def test_recursion_is_marked_not_followed(graph_of):
    g = graph_of(
        {
            "m.py": """
            def walk(n):
                if n:
                    return walk(n - 1)
                return helper()

            def helper():
                return 0
            """
        }
    )
    tree = build_flow(g, "m:walk", depth=5)
    assert names(tree) == ["helper"]  # the self-call is skipped, not looped forever
    mutual = graph_of({"m.py": "def a():\n    return b()\n\ndef b():\n    return a()\n"})
    tree = build_flow(mutual, "m:a", depth=5)
    assert tree.children[0].children[0].repeat == "recursion"


def test_function_shown_twice_is_a_pointer_the_second_time(graph_of):
    g = graph_of(
        {
            "m.py": """
            def log():
                return 1

            def a():
                return log()

            def b():
                return log()

            def main():
                a()
                b()
            """
        }
    )
    tree = build_flow(g, "m:main", depth=3)
    a, b = tree.children
    assert a.children[0].repeat is None and b.children[0].repeat == "above"


def test_size_limits(graph_of):
    calls = "".join(f"    f{i}()\n" for i in range(40))
    defs = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(40))
    g = graph_of({"m.py": defs + "def main():\n" + calls})
    tree = build_flow(g, "m:main", per_step=10)
    assert len(tree.children) == 10 and tree.more == 30
    tree = build_flow(g, "m:main", limit=5)
    assert len(tree.children) == 4 and tree.more == 36  # root counts toward the budget


def test_many_test_callers_fold_into_one_line(graph_of):
    tests = {
        f"tests/test_{i}.py": f"from shop.db import save\n\ndef test_{i}():\n    save(1)\n"
        for i in range(6)
    }
    g = graph_of({**APP, **tests})
    tree = build_flow(g, find(g, "save"), direction="up")
    assert names(tree) == ["checkout"] and tree.tests == 6
    assert "… and 6 in tests, examples or docs" in render_text(tree, "up")


def test_only_test_callers_are_still_shown(graph_of):
    tests = {
        f"tests/test_{i}.py": f"from shop.db import load\n\ndef test_{i}():\n    load(1)\n"
        for i in range(5)
    }
    g = graph_of({"shop/__init__.py": "", "shop/db.py": APP["shop/db.py"], **tests})
    tree = build_flow(g, find(g, "load"), direction="up")
    assert len(tree.children) == 5 and tree.tests == 0


def test_text_rendering(graph_of):
    g = graph_of(APP)
    text = render_text(build_flow(g, find(g, "buy")))
    assert text.splitlines()[0] == "buy  (shop/api.py:4)  · starts here: http"
    assert "├─ load  shop/db.py:4" in text and "└─ save  shop/db.py:1" in text
    leaf = render_text(build_flow(g, find(g, "save")))
    assert "calls nothing else in this project" in leaf


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("shop.cart:checkout", "shop.cart:checkout"),  # full id
        ("checkout", "shop.cart:checkout"),  # plain name
        ("cart.checkout", "shop.cart:checkout"),  # module.function, as Python writes it
        ("shop.cart.checkout", "shop.cart:checkout"),
        ("shop.cart", "shop.cart"),  # module-level code
    ],
)
def test_find_accepts_the_names_people_type(graph_of, query, expected):
    assert find(graph_of(APP), query) == expected


def test_find_prefers_app_code_over_tests(graph_of):
    g = graph_of({**APP, "tests/test_x.py": "def checkout():\n    return 0\n"})
    assert find(g, "checkout") == "shop.cart:checkout"


def test_find_explains_ambiguous_and_unknown_names(graph_of):
    g = graph_of({**APP, "shop/other.py": "def total(x):\n    return x\n"})
    with pytest.raises(FlowError, match="(?s)matches 2 things.*shop.cart:total.*shop.other:total"):
        find(g, "total")
    with pytest.raises(FlowError, match="Did you mean: checkout"):
        find(g, "chekout")


def test_flow_command(make_repo):
    root = make_repo(APP)
    result = runner.invoke(app, ["flow", "checkout", str(root)])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("checkout  (shop/cart.py:6)")
    up = runner.invoke(app, ["flow", "save", str(root), "--up", "--json"])
    data = json.loads(up.output)
    assert data["children"][0]["id"] == "shop.cart:checkout"


def test_flow_command_errors(make_repo):
    root = make_repo(APP)
    unknown = runner.invoke(app, ["flow", "nope", str(root)])
    assert unknown.exit_code == 1 and "No function, class or file named 'nope'" in " ".join(
        unknown.output.split()
    )
    assert runner.invoke(app, ["flow", "checkout", str(root), "--depth", "0"]).exit_code == 2
