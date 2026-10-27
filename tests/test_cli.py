import json

from conftest import SAMPLE_PROJECT
from typer.testing import CliRunner

from wiringmap import __version__
from wiringmap.cli import app

runner = CliRunner()


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"wiringmap {__version__}"


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_scan_writes_default_output_that_ignores_itself(make_repo):
    root = make_repo({"app.py": "def main():\n    pass\n"})
    result = runner.invoke(app, ["scan", str(root)])
    assert result.exit_code == 0, result.output
    graph = json.loads((root / ".wiringmap" / "graph.json").read_text(encoding="utf-8"))
    assert "app:main" in {n["id"] for n in graph["nodes"]}
    assert (root / ".wiringmap" / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"


def test_scan_to_stdout_prints_only_json():
    result = runner.invoke(app, ["scan", str(SAMPLE_PROJECT), "-o", "-", "-q"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["root_name"] == "sample_project"


def test_scan_exclude(make_repo, tmp_path_factory):
    root = make_repo({"app.py": "", "tests/test_app.py": ""})
    out = tmp_path_factory.mktemp("out") / "g.json"
    result = runner.invoke(app, ["scan", str(root), "-o", str(out), "-q", "-e", "tests/"])
    assert result.exit_code == 0
    assert {n["id"] for n in json.loads(out.read_text(encoding="utf-8"))["nodes"]} >= {"app"}
    assert "test_app" not in out.read_text(encoding="utf-8")


def test_scan_reports_unwritable_output_cleanly(make_repo):
    root = make_repo({"app.py": ""})
    blocker = root / "not_a_dir"
    blocker.write_text("")
    result = runner.invoke(app, ["scan", str(root), "-o", str(blocker / "g.json")])
    assert result.exit_code == 1
    assert "Could not write" in result.output
    assert "Traceback" not in result.output


def test_folder_without_python_explains_instead_of_writing_an_empty_map(make_repo):
    root = make_repo({"notes.md": "# hi\n", "data.json": "{}"})
    for command in ("build", "scan"):
        result = runner.invoke(app, [command, str(root)])
        assert result.exit_code == 1
        assert "No Python files found" in result.output
    assert not (root / ".wiringmap").exists()
