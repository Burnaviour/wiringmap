import json


def ids(items):
    return [i.id for i in items]


PRICE = """
def price(items, tax):
    subtotal = sum(i.cost for i in items)
    if subtotal > 100:
        subtotal = subtotal * 0.9
    return round(subtotal * (1 + tax), 2)
"""


def test_no_change_is_empty(diff_of):
    files = {"a.py": PRICE}
    d = diff_of(files, files)
    assert d.empty
    assert d.counts() == {"added": {}, "removed": {}, "changed": {}, "moved": {}}


def test_added_removed_and_changed(diff_of):
    d = diff_of(
        {"a.py": PRICE + "\ndef old():\n    return 1\n"},
        {"a.py": PRICE.replace("0.9", "0.8") + "\ndef new():\n    return 2\n"},
    )
    assert ids(d.added) == ["a:new"]
    assert ids(d.removed) == ["a:old"]
    assert ids(d.changed) == ["a", "a:price"]


def test_docstring_only_edit_is_not_a_change(diff_of):
    with_doc = PRICE.replace("tax):\n", 'tax):\n    """Total with tax."""\n', 1)
    d = diff_of({"a.py": PRICE}, {"a.py": with_doc})
    assert ids(d.changed) == ["a"]  # the file's text changed, the function did not


def test_renamed_function_is_a_move(diff_of):
    d = diff_of({"a.py": PRICE}, {"a.py": PRICE.replace("def price", "def compute_total")})
    assert ids(d.moved) == ["a:compute_total"]
    assert d.moved[0].note == "a:price"
    assert d.added == [] and d.removed == []


def test_function_moved_to_another_file(diff_of):
    d = diff_of({"a.py": PRICE, "b.py": ""}, {"a.py": "", "b.py": PRICE})
    assert ids(d.moved) == ["b:price"]
    assert d.moved[0].note == "a:price"


def test_renamed_file_is_one_move_not_many(diff_of):
    d = diff_of({"pricing.py": PRICE}, {"billing.py": PRICE})
    assert ids(d.moved) == ["billing"]
    assert d.added == [] and d.removed == []


def test_renamed_and_edited_file_shows_as_remove_plus_add(diff_of):
    d = diff_of({"pricing.py": PRICE}, {"billing.py": PRICE.replace("0.9", "0.5")})
    assert "billing" in ids(d.added) and "pricing" in ids(d.removed)


def test_two_identical_new_copies_are_not_guessed_as_a_move(diff_of):
    d = diff_of({"a.py": PRICE}, {"b.py": PRICE, "c.py": PRICE})
    assert "a" in ids(d.removed)
    assert {"b", "c"} <= set(ids(d.added))


def test_new_cycle_and_new_dependency(diff_of):
    d = diff_of(
        {"a.py": "import b\n", "b.py": "X = 1\n"},
        {"a.py": "import b\n", "b.py": "import a\nX = 1\n"},
    )
    assert d.new_cycles == [["a", "b"]]
    assert d.new_dependencies == [("b", "a")]


def test_resolved_cycle(diff_of):
    d = diff_of(
        {"a.py": "import b\n", "b.py": "import a\n"},
        {"a.py": "import b\n", "b.py": "X = 1\n"},
    )
    assert d.resolved_cycles == [["a", "b"]]
    assert d.dropped_dependencies == [("b", "a")]


def test_type_only_import_does_not_make_a_cycle(diff_of):
    d = diff_of(
        {"a.py": "import b\n", "b.py": ""},
        {
            "a.py": "import b\n",
            "b.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import a\n",
        },
    )
    assert d.new_cycles == []


def test_new_third_party_package(diff_of):
    d = diff_of({"a.py": "import json\n"}, {"a.py": "import json\nimport stripe\n"})
    assert [p.name for p in d.new_packages] == ["stripe"]
    assert d.new_packages[0].note == "a.py"


def test_signature_change_lists_callers_that_were_not_updated(diff_of):
    lib = "def create_user(name):\n    return name\n"
    d = diff_of(
        {
            "lib.py": lib,
            "x.py": "from lib import create_user\n\ndef a():\n    return create_user('n')\n",
            "y.py": "from lib import create_user\n\ndef b():\n    return create_user('m')\n",
        },
        {
            "lib.py": lib.replace("(name)", "(name, role)"),
            "x.py": (
                "from lib import create_user\n\ndef a():\n    return create_user('n', 'admin')\n"
            ),
            "y.py": "from lib import create_user\n\ndef b():\n    return create_user('m')\n",
        },
    )
    [change] = d.signature_changes
    assert (change.before, change.after) == ("(name)", "(name, role)")
    assert [c.id for c in change.callers] == ["y:b"]  # untouched: may now be broken
    assert [c.id for c in change.updated_callers] == ["x:a"]


def test_function_that_lost_its_only_caller(diff_of):
    d = diff_of(
        {"a.py": "def helper():\n    return 1\n\ndef main():\n    return helper()\n"},
        {"a.py": "def helper():\n    return 1\n\ndef main():\n    return 2\n"},
    )
    assert ids(d.lost_callers) == ["a:helper"]


def test_new_function_nobody_calls(diff_of):
    d = diff_of(
        {"app.py": "X = 1\n"},
        {
            "app.py": (
                "X = 1\n\ndef orphan():\n    return 1\n\n"
                "@app.get('/')\ndef route():\n    return 2\n"
            ),
            "tests/test_app.py": "def test_x():\n    assert 1\n",
        },
    )
    assert ids(d.unused_new) == ["app:orphan"]  # not the route, not the test


def test_new_copy_of_existing_logic_is_a_duplicate(diff_of):
    copy = (
        PRICE.replace("def price", "def cart_total")
        .replace("subtotal", "acc")
        .replace("items", "rows")
    )
    d = diff_of({"a.py": PRICE}, {"a.py": PRICE, "cart.py": copy})
    [dup] = d.duplicates
    assert [m.id for m in dup.members] == ["cart:cart_total", "a:price"]
    assert dup.new == ["cart:cart_total"]


def test_old_duplicates_are_not_reported_again(diff_of):
    copy = PRICE.replace("def price", "def cart_total")
    files = {"a.py": PRICE, "cart.py": copy, "z.py": "X = 1\n"}
    d = diff_of(files, {**files, "z.py": "X = 2\n"})
    assert d.duplicates == []


def test_duplicates_in_tests_are_ignored(diff_of):
    d = diff_of({"a.py": PRICE}, {"a.py": PRICE, "tests/test_a.py": PRICE.replace("price", "t")})
    assert d.duplicates == []


def test_file_that_stops_parsing_and_one_that_is_repaired(diff_of):
    d = diff_of(
        {"good.py": "x = 1\n", "bad.py": "def f(:\n"},
        {"good.py": "def f(:\n", "bad.py": "x = 1\n"},
    )
    assert ids(d.broken) == ["good"] and d.broken[0].note.startswith("SyntaxError")
    assert ids(d.repaired) == ["bad"]


def test_impacted_files_follow_imports(diff_of):
    before = {"a.py": "import b\n", "b.py": "import c\n", "c.py": "X = 1\n", "d.py": "Y = 1\n"}
    d = diff_of(before, {**before, "c.py": "X = 2\n"})
    assert d.impacted == ["a.py", "b.py"]


def test_method_change_reports_the_method_not_the_class(diff_of):
    cls = "class Cart:\n    def total(self):\n        return 1\n"
    d = diff_of({"a.py": cls}, {"a.py": cls.replace("return 1", "return 2")})
    assert ids(d.changed) == ["a", "a:Cart.total"]


def test_result_is_json_serialisable(diff_of):
    d = diff_of({"a.py": PRICE}, {"a.py": "import a\n" + PRICE, "b.py": PRICE})
    data = json.loads(json.dumps(d.to_dict()))
    assert data["base"] == "before" and "counts" in data


def test_call_that_is_still_there_but_unlinkable_is_not_lost(diff_of):
    # The call survives, but `original` has no known type, so it cannot be linked.
    cls = "class Ctx:\n    def copy(self):\n        return self\n\n"
    d = diff_of(
        {"a.py": cls + "def use(ctx: Ctx):\n    return ctx.copy()\n"},
        {"a.py": cls + "def use(original):\n    with original.copy() as c:\n        return c\n"},
    )
    assert d.lost_callers == []


def test_unchanged_caller_never_counts_as_lost(diff_of):
    # Same caller code; only the callee's file changed around it.
    files = {
        "a.py": "def helper():\n    return 1\n",
        "b.py": "from a import helper\nX = helper()\n",
    }
    d = diff_of(files, {**files, "a.py": "def helper():\n    return 2\n"})
    assert d.lost_callers == []


def test_call_moved_into_another_function_is_not_lost(diff_of):
    cls = "class Ctx:\n    def copy(self):\n        return self\n\n"
    d = diff_of(
        {"a.py": cls + "def deco(ctx: Ctx):\n    return ctx.copy()\n"},
        {
            "a.py": cls
            + "def deco(f):\n"
            + "    def wrapper(original):\n        return original.copy()\n"
            + "    return wrapper\n"
        },
    )
    assert d.lost_callers == []
