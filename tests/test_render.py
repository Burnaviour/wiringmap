import json
import re

from conftest import SAMPLE_PROJECT
from typer.testing import CliRunner

from wiringmap.cli import app
from wiringmap.render import collect_sources, render_report

runner = CliRunner()

DATA = re.compile(r'<script type="application/json" id="wiringmap-data">(.*?)</script>', re.S)


def embedded(page: str) -> dict:
    return json.loads(DATA.search(page).group(1))


def test_report_is_self_contained(sample_graph):
    page = render_report(sample_graph)
    assert page.startswith("<!doctype html>")
    assert "<!--@@" not in page  # every slot was filled
    # Nothing is fetched from the network: no external scripts, styles or fonts.
    assert not re.search(r"<(script|link)[^>]+(src|href)=", page)
    assert "fonts.googleapis" not in page
    for library in ("htmPreact", "cytoscape", "cytoscapeFcose"):
        assert library in page


def test_report_embeds_the_graph(sample_graph):
    data = embedded(render_report(sample_graph))
    assert data["graph"]["root_name"] == "sample_project"
    assert "shop.service:OrderService.place" in {n["id"] for n in data["graph"]["nodes"]}
    assert data["sources"] == {}


def test_hostile_source_cannot_break_out_of_the_page(graph_of):
    code = 'BAD = "</script><script>alert(1)</script><!--"\n\n\ndef f():\n    """</script>"""\n'
    graph = graph_of({"evil.py": code})
    page = render_report(graph, {"evil.py": code})
    # The only closing tags are the page's own three <script> blocks: data, libraries, app.
    assert page.count("</script>") == 3
    data = embedded(page)
    assert data["sources"]["evil.py"] == code
    assert data["graph"]["nodes"][-1]["docstring"] == "</script>"


def test_collect_sources_respects_the_budget(sample_graph):
    sources = collect_sources(SAMPLE_PROJECT, sample_graph)
    assert "src/shop/service.py" in sources
    assert "README.md" not in sources  # only Python files
    assert collect_sources(SAMPLE_PROJECT, sample_graph, limit=10) is None
    assert collect_sources(SAMPLE_PROJECT, sample_graph, limit=None) == sources


def test_build_command_writes_report_and_graph(make_repo):
    root = make_repo({"app.py": "def main():\n    pass\n"})
    result = runner.invoke(app, ["build", str(root), "-q"])
    assert result.exit_code == 0, result.output
    page = (root / ".wiringmap" / "report.html").read_text(encoding="utf-8")
    assert embedded(page)["sources"]["app.py"].startswith("def main")
    assert (
        json.loads((root / ".wiringmap" / "graph.json").read_text(encoding="utf-8"))["root_name"]
        == root.name
    )


def test_build_without_source(make_repo, tmp_path_factory):
    root = make_repo({"app.py": "SECRET = 1\n"})
    out = tmp_path_factory.mktemp("out") / "map.html"
    result = runner.invoke(app, ["build", str(root), "-o", str(out), "--no-source", "-q"])
    assert result.exit_code == 0, result.output
    page = out.read_text(encoding="utf-8")
    assert embedded(page)["sources"] == {}
    assert "SECRET" not in page
    assert not (root / ".wiringmap").exists()  # an explicit -o leaves the repo untouched
