from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from wiringmap.builder import build_graph
from wiringmap.diff import diff_graphs
from wiringmap.graph import CodeGraph

SAMPLE_PROJECT = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture(scope="session")
def sample_graph() -> CodeGraph:
    return build_graph(SAMPLE_PROJECT)


@pytest.fixture
def make_repo(tmp_path: Path):
    """Write ``{relative path: source}`` into a temporary repository and return its root."""

    def make(files: dict[str, str]) -> Path:
        for rel, content in files.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content), encoding="utf-8")
        return tmp_path

    return make


@pytest.fixture
def graph_of(make_repo):
    """Build the graph of a temporary repository made from ``{path: source}``."""

    def build(files: dict[str, str]) -> CodeGraph:
        return build_graph(make_repo(files))

    return build


def edge(graph: CodeGraph, kind: str, source: str, target: str):
    return graph.edges.get((kind, source, target))


def call_targets(graph: CodeGraph, source: str) -> dict[str, str]:
    """``{target: confidence}`` for every call edge leaving ``source``."""
    return {
        e.target: e.confidence
        for e in graph.edges.values()
        if e.kind == "calls" and e.source == source
    }


class GitRepo:
    """A throwaway git repository for tests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        import subprocess

        done = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    def write(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content), encoding="utf-8")

    def remove(self, *paths: str) -> None:
        for rel in paths:
            (self.root / rel).unlink()

    def commit(self, files: dict[str, str] | None = None, message: str = "change") -> str:
        if files:
            self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    root = tmp_path / "project"
    root.mkdir()
    return GitRepo(root)


@pytest.fixture
def diff_of(tmp_path):
    """Diff two versions of a project given as {path: source} dicts."""

    def make(before: dict[str, str], after: dict[str, str]):
        graphs = []
        for side, files in (("before", before), ("after", after)):
            root = tmp_path / side / "proj"  # same folder name on both sides, like real use
            root.mkdir(parents=True)
            for rel, content in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(textwrap.dedent(content), encoding="utf-8")
            graphs.append(build_graph(root))
        return diff_graphs(graphs[0], graphs[1], "before", "after")

    return make
