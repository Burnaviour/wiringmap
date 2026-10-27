# Changelog

## Unreleased

### Added

- `wiringmap diff`: what changed in the code's structure between your last commit and
  your current files, or between any two commits, branches or tags. It flags files that
  stopped parsing, new import cycles, breaking signature changes with callers that were
  not updated, copied logic, functions that lost all their callers and new third-party
  packages, and lists every change file by file. Output as a terminal summary,
  Markdown, JSON, or on the map (`--open`).
- The graph records a `hash` and a `shape` fingerprint for code, and the names of calls
  it could not link.

### Fixed

- Running wiringmap in a folder without Python files now explains what happened
  instead of writing an empty map.

## 0.1.0 (2026-10-16)

First public preview.

### Added

- `wiringmap build`: an interactive, offline map of a Python project in one HTML file,
  with a folder map, file and function details, search, source view, entry points and
  web routes, and light and dark themes.
- `wiringmap scan`: the code graph as `graph.json` (format in `docs/graph-schema.md`).
- Python analysis: imports, relative imports, re-exports, inheritance, decorators, and
  call links marked resolved, inferred or guessed. Files written with Python 3.14
  syntax parse on older Python versions too.
- Scanning honours `.gitignore` files at every level and `.git/info/exclude`.
