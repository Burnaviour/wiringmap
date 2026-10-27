import json
import re

from typer.testing import CliRunner

from wiringmap.cli import app

runner = CliRunner()

SHOP = {
    "shop/__init__.py": "",
    "shop/cart.py": "def total(items):\n    return sum(items)\n",
    "shop/api.py": "from shop.cart import total\n\ndef checkout(items):\n    return total(items)\n",
}


def run(*args):
    return runner.invoke(app, ["diff", *map(str, args)])


def said(result) -> str:
    """The output with line wrapping undone, since rich wraps long messages."""
    return " ".join(result.output.split())


def test_compares_your_current_files_with_the_last_commit(git_repo):
    git_repo.commit(SHOP)
    git_repo.write({"shop/cart.py": SHOP["shop/cart.py"] + "\ndef discount(x):\n    return x\n"})
    result = run(git_repo.root, "--markdown")
    assert result.exit_code == 0, result.output
    assert "`HEAD (" in result.output and "your current files" in result.output
    assert "`shop/cart.py`: added `discount`" in result.output


def test_untracked_new_files_count_and_ignored_ones_do_not(git_repo):
    git_repo.commit({**SHOP, ".gitignore": "scratch/\n"})
    git_repo.write({"shop/new.py": "X = 1\n", "scratch/tmp.py": "Y = 1\n"})
    out = run(git_repo.root, "--json")
    data = json.loads(out.output)
    assert [i["id"] for i in data["added"]] == ["shop.new"]


def test_json_output(git_repo):
    git_repo.commit(SHOP)
    git_repo.write({"shop/cart.py": "def total(items, tax):\n    return sum(items) * tax\n"})
    data = json.loads(run(git_repo.root, "--json").output)
    assert data["counts"]["changed"] == {"module": 1, "function": 1}
    [sig] = data["signature_changes"]
    assert sig["breaking"] is True
    assert [(c["id"], c["path"]) for c in sig["callers"]] == [("shop.api:checkout", "shop/api.py")]


def test_two_commits_without_touching_the_working_tree(git_repo):
    git_repo.commit(SHOP)
    git_repo.commit({"shop/extra.py": "def helper():\n    return 1\n"})
    git_repo.write({"shop/uncommitted.py": "Z = 1\n"})  # must not show up
    data = json.loads(run(git_repo.root, "--base", "HEAD~1", "--head", "HEAD", "--json").output)
    assert [i["id"] for i in data["added"]] == ["shop.extra", "shop.extra:helper"]
    assert re.match(r"HEAD \([0-9a-f]{7}\)", data["head"])


def test_no_changes(git_repo):
    git_repo.commit(SHOP)
    result = run(git_repo.root, "--markdown")
    assert result.exit_code == 0
    assert "No changes to Python code." in result.output


def test_subfolder_of_a_repository(git_repo):
    git_repo.commit({"backend/app/main.py": "X = 1\n", "other/tool.py": "Y = 1\n"})
    git_repo.write({"backend/app/main.py": "X = 2\n", "other/tool.py": "Y = 2\n"})
    data = json.loads(run(git_repo.root / "backend", "--json").output)
    assert [i["path"] for i in data["changed"]] == ["app/main.py"]


def test_writes_markdown_json_and_html_files(git_repo, tmp_path):
    git_repo.commit(SHOP)
    git_repo.write({"shop/cart.py": "def total(items):\n    return max(items)\n"})
    for name in ("d.md", "d.json", "d.html"):
        result = run(git_repo.root, "-q", "-o", tmp_path / name)
        assert result.exit_code == 0, result.output
    assert "changed `total`" in (tmp_path / "d.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))["changed"]
    page = (tmp_path / "d.html").read_text(encoding="utf-8")
    assert '"diff":{' in page and "shop.cart:total" in page


def test_open_writes_the_map_and_opens_it(git_repo, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    git_repo.commit(SHOP)
    git_repo.write({"shop/cart.py": "def total(items):\n    return 0\n"})
    result = run(git_repo.root, "-q", "--open")
    assert result.exit_code == 0, result.output
    assert (git_repo.root / ".wiringmap" / "diff.html").exists()
    assert opened and opened[0].endswith("/.wiringmap/diff.html")


def test_outside_git_is_explained(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    result = run(tmp_path)
    assert result.exit_code == 1
    assert "not inside a git repository" in said(result)


def test_unknown_ref_and_empty_repository_are_explained(git_repo):
    git_repo.write(SHOP)
    result = run(git_repo.root)
    assert result.exit_code == 1 and "no commits yet" in said(result)
    git_repo.commit()
    result = run(git_repo.root, "--base", "does-not-exist")
    assert result.exit_code == 1 and "does-not-exist" in said(result)


def test_conflicting_options_are_rejected(git_repo):
    git_repo.commit(SHOP)
    assert run(git_repo.root, "--json", "--markdown").exit_code == 2
    result = run(git_repo.root, "-o", "diff.txt")
    assert result.exit_code == 2 and ".html, .md or .json" in said(result)


def test_markdown_survives_a_non_utf8_terminal(git_repo, monkeypatch):
    # Windows with output redirected to a file: the default code page is cp1252.
    import io
    import sys

    from wiringmap.cli import _utf8_output

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    _utf8_output()
    print("old → new ├─ …", file=sys.stdout)
    sys.stdout.flush()
    assert raw.getvalue().decode("utf-8").strip() == "old → new ├─ …"
