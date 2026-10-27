import os
import sys

import pytest

from wiringmap.gitsnap import GitError, checkout, locate, resolve, submodules


def files_in(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_checkout_holds_the_committed_version_not_the_working_tree(git_repo):
    first = git_repo.commit({"app.py": "x = 1\n", "lib/util.py": "y = 2\n"})
    git_repo.write({"app.py": "x = 999\n", "new.py": "z = 3\n"})  # uncommitted edits
    loc = locate(git_repo.root)
    with checkout(loc, resolve(loc, "HEAD")) as root:
        assert root.name == "project"
        assert files_in(root) == ["app.py", "lib/util.py"]
        assert (root / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert resolve(loc, "HEAD") == first
    assert not root.exists()  # cleaned up afterwards


def test_checkout_skips_binaries_but_keeps_gitignore_and_config(git_repo):
    git_repo.commit(
        {"app.py": "", ".gitignore": "build/\n", "pyproject.toml": "", "logo.png": "PNG"}
    )
    loc = locate(git_repo.root)
    with checkout(loc, resolve(loc, "HEAD")) as root:
        assert files_in(root) == [".gitignore", "app.py", "pyproject.toml"]


def test_export_ignore_does_not_hide_files(git_repo):
    # git archive would drop tests/ here and make every test file look newly added.
    git_repo.commit({".gitattributes": "tests/ export-ignore\n", "tests/test_a.py": "", "a.py": ""})
    loc = locate(git_repo.root)
    with checkout(loc, resolve(loc, "HEAD")) as root:
        assert "tests/test_a.py" in files_in(root)


def test_subfolder_is_checked_out_under_its_own_name(git_repo):
    git_repo.commit({"backend/app/main.py": "", "frontend/index.ts": ""})
    loc = locate(git_repo.root / "backend")
    assert loc.prefix == "backend"
    with checkout(loc, resolve(loc, "HEAD")) as root:
        assert root.name == "backend"
        assert files_in(root) == ["app/main.py"]


def test_resolve_branches_tags_and_relative_refs(git_repo):
    first = git_repo.commit({"a.py": "1"})
    git_repo.git("tag", "v1")
    second = git_repo.commit({"a.py": "2"})
    loc = locate(git_repo.root)
    assert resolve(loc, "v1") == first
    assert resolve(loc, "HEAD~1") == first
    assert resolve(loc, "main") == second


def test_unknown_ref_is_explained(git_repo):
    git_repo.commit({"a.py": ""})
    with pytest.raises(GitError, match="does not know .*nope"):
        resolve(locate(git_repo.root), "nope")


def test_repository_without_commits_is_explained(git_repo):
    with pytest.raises(GitError, match="no commits yet"):
        resolve(locate(git_repo.root), "HEAD")


def test_folder_outside_git_is_explained(tmp_path):
    with pytest.raises(GitError, match="not inside a git repository"):
        locate(tmp_path)


def test_submodules_are_found_and_left_out(git_repo):
    git_repo.commit({"a.py": ""})
    fake = git_repo.git("rev-parse", "HEAD").strip()
    git_repo.git("update-index", "--add", "--cacheinfo", f"160000,{fake},vendor/lib")
    git_repo.git("commit", "-q", "-m", "add submodule")  # no `add -A`: it would drop the gitlink
    commit = git_repo.git("rev-parse", "HEAD").strip()
    loc = locate(git_repo.root)
    assert submodules(loc) == ["vendor/lib"]
    assert submodules(loc, commit) == ["vendor/lib"]
    with checkout(loc, commit) as root:
        assert files_in(root) == ["a.py"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinks_are_left_out(git_repo):
    git_repo.write({"a.py": ""})
    os.symlink("a.py", git_repo.root / "link.py")
    commit = git_repo.commit()
    loc = locate(git_repo.root)
    with checkout(loc, commit) as root:
        assert files_in(root) == ["a.py"]


@pytest.mark.skipif(sys.platform != "linux", reason="needs a filesystem that allows any bytes")
def test_non_utf8_file_names_do_not_crash(git_repo):
    name = os.fsdecode(b"caf\xe9.py")
    (git_repo.root / name).write_text("x = 1\n")
    commit = git_repo.commit()
    loc = locate(git_repo.root)
    with checkout(loc, commit) as root:
        assert len(files_in(root)) == 1
