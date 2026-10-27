"""Read a past version of a repository out of git, without touching the working tree."""

from __future__ import annotations

import posixpath
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from wiringmap.scanner import detect_language

_SUBMODULE = "160000"
_SYMLINK = "120000"


class GitError(Exception):
    """A git problem explained in words the user can act on."""


@dataclass(frozen=True)
class RepoLocation:
    toplevel: Path  # the repository's root folder
    prefix: str  # the scanned folder inside it: "" for the root, else "a/b"


def _git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",  # file names need not be valid UTF-8
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH.") from None
    if done.returncode != 0:
        raise GitError(done.stderr.strip() or f"git {' '.join(args)} failed")
    return done.stdout


def locate(path: Path) -> RepoLocation:
    """Where ``path`` sits in its git repository."""
    try:
        toplevel = _git(path, "rev-parse", "--show-toplevel").strip()
    except GitError:
        raise GitError(
            f"{path} is not inside a git repository. wiringmap diff compares versions "
            "that git knows about; run `git init` and commit once first."
        ) from None
    prefix = _git(path, "rev-parse", "--show-prefix").strip().rstrip("/")
    return RepoLocation(Path(toplevel), prefix)


def resolve(loc: RepoLocation, ref: str) -> str:
    """The commit id ``ref`` points to."""
    try:
        return _git(loc.toplevel, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except GitError:
        if ref == "HEAD" and not _has_commits(loc):
            raise GitError(
                "This repository has no commits yet. Commit once, and wiringmap diff will "
                "compare your later changes against it."
            ) from None
        raise GitError(f"git does not know the commit, branch or tag '{ref}'.") from None


def _has_commits(loc: RepoLocation) -> bool:
    try:
        _git(loc.toplevel, "rev-parse", "--verify", "--quiet", "HEAD")
        return True
    except GitError:
        return False


def submodules(loc: RepoLocation, commit: str | None = None) -> list[str]:
    """Submodule paths relative to the scanned folder, in ``commit`` or in the index."""
    if commit is None:
        listing = _git(loc.toplevel, "ls-files", "--stage", "-z")
        entries = [line.split(" ", 1)[0] + "\t" + line.split("\t", 1)[1] for line in _nul(listing)]
    else:
        entries = [_tree_entry(line) for line in _nul(_ls_tree(loc, commit))]
    paths = []
    for entry in entries:
        mode, path = entry.split("\t", 1)
        if mode.split(" ")[0] == _SUBMODULE:
            relative = _relative(loc, path)
            if relative is not None:
                paths.append(relative)
    return paths


@contextmanager
def checkout(loc: RepoLocation, commit: str) -> Iterator[Path]:
    """A temporary folder holding the scanned folder as it was in ``commit``.

    Only files the scanner reads (source, config and ``.gitignore``) are written;
    submodules and symlinks are left out, matching what a scan of the working tree sees.
    The folder keeps the real folder's name, because module names can depend on it.
    """
    tmp = Path(tempfile.mkdtemp(prefix="wiringmap-"))
    try:
        root = tmp / (loc.prefix.rsplit("/", 1)[-1] if loc.prefix else loc.toplevel.name)
        root.mkdir()
        wanted: list[tuple[str, str]] = []  # (blob id, path relative to root)
        for line in _nul(_ls_tree(loc, commit)):
            mode, kind, blob, path = _split_tree_line(line)
            if kind != "blob" or mode in (_SUBMODULE, _SYMLINK):
                continue
            relative = _relative(loc, path)
            name = posixpath.basename(path)
            if relative and (name == ".gitignore" or detect_language(name)):
                wanted.append((blob, relative))
        _write_blobs(loc, wanted, root)
        yield root
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def describe(ref: str, commit: str) -> str:
    """A short label such as "HEAD (3f2a1bc)" or "main (9e0d44a)"."""
    return f"{ref} ({commit[:7]})"


# Helpers ----------------------------------------------------------------------------


def _ls_tree(loc: RepoLocation, commit: str) -> str:
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if loc.prefix:
        args += ["--", loc.prefix]
    return _git(loc.toplevel, *args)


def _nul(text: str) -> list[str]:
    return [part for part in text.split("\0") if part]


def _split_tree_line(line: str) -> tuple[str, str, str, str]:
    meta, path = line.split("\t", 1)
    mode, kind, blob = meta.split(" ")
    return mode, kind, blob, path


def _tree_entry(line: str) -> str:
    mode, _, _, path = _split_tree_line(line)
    return f"{mode}\t{path}"


def _relative(loc: RepoLocation, path: str) -> str | None:
    if not loc.prefix:
        return path
    if path.startswith(loc.prefix + "/"):
        return path[len(loc.prefix) + 1 :]
    return None


def _write_blobs(loc: RepoLocation, wanted: list[tuple[str, str]], root: Path) -> None:
    """Fetch many blobs through one ``git cat-file --batch`` process."""
    if not wanted:
        return
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=loc.toplevel,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin and proc.stdout
    try:
        for blob, relative in wanted:
            # One request, one answer, so neither pipe can fill up and deadlock.
            proc.stdin.write(blob.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) < 3 or header[1] != b"blob":
                raise GitError(f"git could not read {relative} from its history.")
            data = proc.stdout.read(int(header[2]))
            proc.stdout.read(1)  # the newline after each object
            parts = relative.split("/")
            if any(part in ("", ".", "..") for part in parts):
                continue  # never write outside the temporary folder
            target = root.joinpath(*parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError:
                continue  # a name this OS cannot store (e.g. "aux.py" on Windows)
    finally:
        proc.stdin.close()
        proc.wait()
