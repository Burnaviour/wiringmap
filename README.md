# wiringmap

**See how your code is wired.** wiringmap turns any Python project into an interactive
map: what every file uses, what uses it, and what breaks if you change it. It runs
offline, produces one HTML file, and needs no AI.

[![PyPI](https://img.shields.io/pypi/v/wiringmap)](https://pypi.org/project/wiringmap/)
[![CI](https://github.com/Burnaviour/wiringmap/actions/workflows/ci.yml/badge.svg)](https://github.com/Burnaviour/wiringmap/actions/workflows/ci.yml)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)](https://pypi.org/project/wiringmap/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Status: early](https://img.shields.io/badge/status-early%20preview-orange)

![wiringmap exploring a FastAPI app: the folder map, what login.py uses, the 16 files that depend on models.py, search, a function's callers and callees, and its source](docs/images/demo.gif)

<sub>Shown above: <a href="https://github.com/fastapi/full-stack-fastapi-template">fastapi/full-stack-fastapi-template</a>.</sub>

## Why

An AI agent wrote most of your code. It works, but you have never read most of it, and
now you need to change something. Which file does what? What calls this function? What
breaks if you touch it?

wiringmap answers those questions in seconds, and your code never leaves your machine.

## Try it

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). Then, inside
any Python project:

```bash
uvx wiringmap build . --open
```

Or install it: `pip install wiringmap` (or `uv tool install wiringmap`), then run
`wiringmap build . --open`.

This writes `.wiringmap/report.html` and opens it in your browser. The `.wiringmap/`
folder ignores itself, so it is never committed by accident.

## What you get

- **A map of the whole project.** Files are boxes inside their folders, and arrows show
  imports. Switch to **Calls** to see which files call into which; thicker arrows mean
  more calls. Large projects start with folders closed (double-click to open one), and
  tests, docs and migrations stay closed so you see the application first.
- **Click a file** to see what it uses (indigo), what uses it (amber), its docstring,
  and the classes and functions inside it.
- **Click a function** to see what it calls and what calls it. Each link says how it
  was found: *resolved* from names and imports, *inferred* from type hints, or
  *guessed* from a unique method name.
- **"Start reading here"** lists where the program starts: commands, scripts, and web
  routes with their method and path (`POST /login/access-token`).
- **Search** every file, class and function with <kbd>/</kbd>, and **read the source**
  next to the map.
- **Big projects work too.** pandas (1,411 files) scans in under 8 seconds, and its
  report opens in under one.

## See what your AI agent changed

After an agent has edited your code, ask what changed in the *structure*, not just
which lines:

```bash
wiringmap diff            # your current files vs. your last commit
wiringmap diff --open     # the same, on the map
wiringmap diff --base main --head my-branch
```

It reports, in plain words:

- **What needs a look:** files that no longer parse, new import cycles, signature
  changes whose callers were not updated (and whether old calls will actually fail),
  logic copied from an existing function, functions that lost all their callers, and
  new third-party packages.
- **What changed, file by file:** added, changed, deleted, and renamed or moved
  functions and classes. Moves and renames are recognised from the code itself, even
  across files.
- **What depends on it:** the other files that import the changed ones.

`--markdown` prints the summary for a pull request or an AI agent, and `--json` gives
tools the full result. Nothing is checked out or changed: the old version is read
straight from git.

## Private by design

- wiringmap **reads** your code; it never runs it.
- Nothing is uploaded. The report is one self-contained file that makes no network
  requests, so you can open it offline or send it to a teammate.
- `--no-source` leaves your source code out of the report, keeping only its structure.

## Options

| Option | Meaning |
|---|---|
| `--open` | Open the report in your browser |
| `-o FILE` | Write the report somewhere else, leaving the project untouched |
| `-e PATTERN` | Skip extra paths, gitignore style (`-e "scripts/"`). `.gitignore` is always honoured |
| `--source / --no-source` | Include the source code in the report. By default it is included when the project has under 8 MB of Python |
| `-q` | No progress bar or summary |

`wiringmap scan .` writes only the data, `graph.json`: every file, class and function
plus the imports, calls, inheritance and decorators between them. Its format is
documented in [docs/graph-schema.md](docs/graph-schema.md), so you can build your own
tools on it.

## Roadmap

- [x] Code graph for Python, with call links that say how sure they are
- [x] Interactive offline map with search and source view
- [x] **`wiringmap diff`:** what an AI agent (or anyone) changed in the structure
- [ ] **Flow view:** follow a command or web request step by step through the code
- [ ] **Framework wiring:** FastAPI `Depends`, callbacks and overridden methods as links
- [ ] **Plain-English explanations** of files, folders and flows from a local model
      (Ollama), optional
- [ ] JavaScript and TypeScript

## Contributing

Bug reports and ideas are welcome: please [open an issue](https://github.com/Burnaviour/wiringmap/issues).
To work on the code, see [CONTRIBUTING.md](CONTRIBUTING.md) and
[how wiringmap works](docs/architecture.md).

If wiringmap helps you, a ⭐ helps other people find it.

## License

[MIT](LICENSE). Reports bundle [Preact](https://preactjs.com) (MIT),
[htm](https://github.com/developit/htm) (Apache-2.0),
[Cytoscape.js](https://js.cytoscape.org) and
[cytoscape-fcose](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose) (MIT); see
[their licenses](src/wiringmap/render/static/vendor/licenses).
