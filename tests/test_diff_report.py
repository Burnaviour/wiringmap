from wiringmap.diff_report import to_markdown

LIB = "def create_user(name):\n    return name\n"


def report(diff_of, before, after):
    d = diff_of(before, after)
    return to_markdown(d), d


def test_no_changes_says_so(diff_of):
    text, _ = report(diff_of, {"a.py": "x = 1\n"}, {"a.py": "x = 1\n"})
    assert "No changes to Python code." in text
    assert "Needs a look" not in text


def test_changes_are_grouped_by_file_with_new_files_first(diff_of):
    text, _ = report(
        diff_of,
        {"app/a.py": "def f():\n    return 1\n", "app/__init__.py": ""},
        {
            "app/a.py": "def f():\n    return 2\n",
            "app/__init__.py": "",
            "app/z_new.py": "def g():\n    return 3\n",
        },
    )
    files = [line for line in text.splitlines() if line.startswith("- `app/")]
    assert files == ["- `app/z_new.py` (new file): added `g`", "- `app/a.py`: changed `f`"]


def test_tests_come_after_app_code(diff_of):
    text, _ = report(
        diff_of,
        {"a.py": "X = 1\n", "tests/test_a.py": "Y = 1\n"},
        {"a.py": "X = 2\n", "tests/test_a.py": "Y = 2\n"},
    )
    assert text.index("`a.py`") < text.index("`tests/test_a.py`")


def test_nested_helpers_fold_under_their_new_parent(diff_of):
    nested = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    text, _ = report(diff_of, {"a.py": "X = 1\n"}, {"a.py": "X = 1\n" + nested})
    assert "added `outer`" in text and "outer.inner" not in text


def test_breaking_signature_warns_and_counts_tests_separately(diff_of):
    caller = "from lib import create_user\n\ndef {n}():\n    return create_user('x')\n"
    files = {
        "lib.py": LIB,
        "app.py": caller.format(n="signup"),
        "tests/test_lib.py": caller.format(n="test_it"),
    }
    text, _ = report(diff_of, files, {**files, "lib.py": LIB.replace("(name)", "(name, role)")})
    assert "Signature changed, old calls will fail" in text
    assert "`signup` (app.py:3) and 1 test(s)" in text


def test_compatible_signature_does_not_warn(diff_of):
    caller = "from lib import create_user\n\ndef signup():\n    return create_user('x')\n"
    files = {"lib.py": LIB, "app.py": caller}
    text, _ = report(
        diff_of, files, {**files, "lib.py": LIB.replace("(name)", "(name, role='user')")}
    )
    assert "Needs a look" not in text
    assert "same parameters or new optional ones" in text


def test_broken_file_and_rename_lines(diff_of):
    text, _ = report(
        diff_of,
        {"a.py": "def old_name():\n    return 1\n", "b.py": "x = 1\n"},
        {"a.py": "def new_name():\n    return 1\n", "b.py": "def oops(:\n"},
    )
    assert "- `a.py`: renamed `old_name` → `new_name`" in text
    assert "- `b.py` (no longer parses)" in text
    assert "**No longer parses:** `b.py`" in text


def test_long_lists_are_cut(diff_of):
    many = {f"m{i}.py": f"X = {i}\n" for i in range(40)}
    text, _ = report(diff_of, many, {k: v.replace("X", "Y") for k, v in many.items()})
    assert "and 16 more files" in text


def test_a_file_with_many_new_functions_stays_readable(diff_of):
    many = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(20))
    text, _ = report(diff_of, {"a.py": "X = 1\n"}, {"a.py": "X = 1\n" + many})
    [line] = [line for line in text.splitlines() if line.startswith("- `a.py`")]
    assert line.endswith("and 12 more")
