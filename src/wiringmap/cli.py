"""Command-line entry point."""

from __future__ import annotations

import json
import sys
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.table import Table

from wiringmap import __version__, gitsnap
from wiringmap.builder import build_graph
from wiringmap.diff import diff_graphs
from wiringmap.diff_report import to_markdown
from wiringmap.graph import CodeGraph, NodeKind
from wiringmap.render.html import SOURCE_BUDGET, collect_sources, render_report

OUTPUT_DIR = ".wiringmap"

app = typer.Typer(
    name="wiringmap",
    help="See how your code is wired: offline, interactive, AI optional.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
err = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wiringmap {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """wiringmap turns a repository into an interactive map of its files, classes and functions."""
    _utf8_output()


def _utf8_output() -> None:
    """Write UTF-8 even when output is redirected on Windows, where the default code page
    cannot encode the arrows and tree lines in reports (``wiringmap diff --markdown > x.md``)."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            # Some streams cannot change encoding mid-way; leave those as they are.
            with suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")


@app.command()
def scan(
    path: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, resolve_path=True, help="Repository to scan."),
    ] = Path("."),
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help=f"Output file, or '-' for stdout. Default: <path>/{OUTPUT_DIR}/graph.json",
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", "-e", help="Extra gitignore-style pattern to skip. Repeatable."),
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print errors.")] = False,
) -> None:
    """Analyse a repository and write its code graph (graph.json)."""
    graph, elapsed = _scan(path, exclude, quiet)
    _require_python(graph, path)
    if output == "-":
        typer.echo(graph.to_json())
    else:
        target = Path(output) if output else _output_dir(path) / "graph.json"
        _write(target, graph.to_json() + "\n", quiet)
    if not quiet:
        _print_summary(graph, elapsed)


@app.command()
def build(
    path: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, resolve_path=True, help="Repository to map."),
    ] = Path("."),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help=f"Report file. Default: <path>/{OUTPUT_DIR}/report.html"
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", "-e", help="Extra gitignore-style pattern to skip. Repeatable."),
    ] = None,
    source: Annotated[
        bool | None,
        typer.Option(
            "--source/--no-source",
            help="Embed source code so the report can show it. Default: only up to 8 MB of Python.",
        ),
    ] = None,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open the report in your browser when done.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print errors.")] = False,
) -> None:
    """Build an interactive map of a repository as one offline HTML file."""
    graph, elapsed = _scan(path, exclude, quiet)
    _require_python(graph, path)
    sources = None
    if source is not False:
        sources = collect_sources(path, graph, limit=None if source else SOURCE_BUDGET)
        if sources is None and not quiet:
            err.print(
                "[yellow]Source code not embedded:[/yellow] more than "
                f"{SOURCE_BUDGET // 1_000_000} MB of Python. Use --source to include it anyway."
            )
    if output is None:
        out_dir = _output_dir(path)
        _write(out_dir / "graph.json", graph.to_json() + "\n", quiet=True)
        target = out_dir / "report.html"
    else:
        target = output
    _write(target, render_report(graph, sources), quiet)
    if not quiet:
        _print_summary(graph, elapsed)
    if open_report:
        webbrowser.open(target.resolve().as_uri())


@app.command()
def diff(
    path: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, resolve_path=True, help="Project to compare."),
    ] = Path("."),
    base: Annotated[
        str, typer.Option("--base", "-b", help="Compare against this commit, branch or tag.")
    ] = "HEAD",
    head: Annotated[
        str | None,
        typer.Option(
            "--head", help="Compare with this commit, branch or tag instead of your current files."
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Also write the result: .html (map), .md or .json."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of the summary (for tools).")
    ] = False,
    as_markdown: Annotated[
        bool, typer.Option("--markdown", help="Print Markdown (for pull requests and agents).")
    ] = False,
    open_report: Annotated[
        bool, typer.Option("--open", help="Show the changes on the map in your browser.")
    ] = False,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", "-e", help="Extra gitignore-style pattern to skip. Repeatable."),
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Print nothing on success.")] = False,
) -> None:
    """Show what changed in the code's structure, by default since your last commit."""
    if as_json and as_markdown:
        err.print("[red]Choose one of --json and --markdown.[/red]")
        raise typer.Exit(2)
    if output is not None and output.suffix.lower() not in (".html", ".md", ".json"):
        err.print("[red]--output must end in .html, .md or .json.[/red]")
        raise typer.Exit(2)
    want_html = open_report or (output is not None and output.suffix.lower() == ".html")
    try:
        loc = gitsnap.locate(path)
        base_commit = gitsnap.resolve(loc, base)
        head_commit = gitsnap.resolve(loc, head) if head else None
        submodules = set(gitsnap.submodules(loc)) | set(gitsnap.submodules(loc, base_commit))
        if head_commit:
            submodules |= set(gitsnap.submodules(loc, head_commit))
        skip = [*(exclude or []), *(f"/{s}/" for s in sorted(submodules))]
        with err.status("Comparing…", spinner="dots") if not quiet else _nothing():
            before, _ = _graph_at(loc, base_commit, skip, want_sources=False)
            if head_commit:
                after, sources = _graph_at(loc, head_commit, skip, want_sources=want_html)
            else:
                after = build_graph(path, skip)
                sources = collect_sources(path, after) if want_html else None
    except gitsnap.GitError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None

    if not _has_python(before) and not _has_python(after):
        _require_python(after, path)
    head_label = (
        gitsnap.describe(head, head_commit) if head and head_commit else "your current files"
    )
    result = diff_graphs(before, after, gitsnap.describe(base, base_commit), head_label)
    markdown = to_markdown(result, after)

    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    elif as_markdown:
        typer.echo(markdown, nl=False)
    elif not quiet:
        Console().print(Markdown(markdown))

    if output is not None:
        suffix = output.suffix.lower()
        if suffix == ".json":
            _write(output, json.dumps(result.to_dict(), indent=2) + "\n", quiet=True)
        elif suffix == ".md":
            _write(output, markdown, quiet=True)
        else:
            _write(output, render_report(after, sources, diff=result), quiet=True)
    if open_report:
        target = output if output is not None and output.suffix.lower() == ".html" else None
        if target is None:
            target = _output_dir(path) / "diff.html"
            _write(target, render_report(after, sources, diff=result), quiet=True)
        webbrowser.open(target.resolve().as_uri())


def _graph_at(
    loc: gitsnap.RepoLocation, commit: str, skip: list[str], want_sources: bool
) -> tuple[CodeGraph, dict | None]:
    with gitsnap.checkout(loc, commit) as root:
        graph = build_graph(root, skip)
        return graph, collect_sources(root, graph) if want_sources else None


def _has_python(graph: CodeGraph) -> bool:
    return any(node.kind == NodeKind.MODULE for node in graph.nodes.values())


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def _scan(path: Path, exclude: list[str] | None, quiet: bool) -> tuple[CodeGraph, float]:
    started = time.perf_counter()
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=err,
        transient=True,
        disable=quiet,
    ) as bar:
        task = bar.add_task("Parsing Python files", total=None)
        graph = build_graph(
            path,
            exclude or [],
            progress=lambda done, total: bar.update(task, completed=done, total=total),
        )
    return graph, time.perf_counter() - started


def _require_python(graph: CodeGraph, path: Path) -> None:
    """Stop with a clear message instead of writing an empty map."""
    if any(node.kind == NodeKind.MODULE for node in graph.nodes.values()):
        return
    err.print(
        f"[yellow]No Python files found in {path}[/yellow] (files listed in .gitignore are "
        "skipped).\nwiringmap maps Python projects: run it inside one, or pass its folder:\n"
        "  wiringmap build path/to/project --open"
    )
    raise typer.Exit(1)


def _output_dir(repo: Path) -> Path:
    out_dir = repo / OUTPUT_DIR
    out_dir.mkdir(exist_ok=True)
    # Like .pytest_cache: the folder ignores itself, so it is never committed by accident.
    ignore = out_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Created by wiringmap\n*\n", encoding="utf-8")
    return out_dir


def _write(target: Path, text: str, quiet: bool) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        err.print(f"[red]Could not write {target}:[/red] {exc.strerror or exc}")
        raise typer.Exit(1) from None
    if not quiet:
        size = len(text.encode("utf-8"))
        err.print(f"[green]Wrote[/green] {target} ({_human_size(size)})")


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"  # pragma: no cover


def _print_summary(graph: CodeGraph, elapsed: float) -> None:
    stats = graph.stats()
    table = Table(title=f"{graph.root_name} — scanned in {elapsed:.1f}s", title_justify="left")
    table.add_column("What")
    table.add_column("Count", justify="right")
    for kind, count in stats["nodes"].items():
        table.add_row(f"{kind} nodes", str(count))
    for kind, count in stats["edges"].items():
        table.add_row(f"{kind} edges", str(count))
    calls = stats["calls_by_confidence"]
    if calls:
        table.add_row("calls", ", ".join(f"{n} {conf}" for conf, n in calls.items()))
    if stats["parse_errors"]:
        table.add_row("[yellow]files not parsed[/yellow]", str(stats["parse_errors"]))
    err.print(table)
