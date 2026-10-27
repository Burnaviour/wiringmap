# `graph.json` format

`wiringmap scan` writes the code graph as one JSON file. Every other wiringmap feature
(the HTML viewer, AI summaries, diffs) reads this file, and you can read it too.

The current schema version is **1**. Nodes and edges are sorted, so scanning the same
code twice gives the same file apart from `created_at`. That makes the file safe to
diff and to commit.

## Top level

```json
{
  "schema_version": 1,
  "generator": {"name": "wiringmap", "version": "0.1.0"},
  "root_name": "my-project",
  "created_at": "2026-10-13T20:00:00+00:00",
  "meta": {"file_count": 42, "languages": {"python": 30, "markdown": 5}},
  "stats": {"nodes": {...}, "edges": {...}, "calls_by_confidence": {...}, "parse_errors": 0},
  "nodes": [...],
  "edges": [...]
}
```

The file stores only the repository folder's name, never its absolute path.

## Nodes

| Field | Meaning |
|---|---|
| `id` | Unique id (scheme below) |
| `kind` | `directory`, `file`, `module`, `class`, `function`, `method`, `external` |
| `name` | Short display name: `models.py`, `Order`, `total` |
| `path` | File or directory path relative to the repository root (forward slashes) |
| `line`, `end_line`, `loc` | Where a class or function sits; `loc` for modules counts non-blank lines |
| `docstring` | First 1000 characters of the docstring |
| `signature` | Functions and methods: `(self, order: Order) -> str` |
| `attrs` | Extra facts, listed below |

Empty fields are left out.

### Id scheme

| Kind | Id | Example |
|---|---|---|
| directory | `dir:<path>` (`dir:.` is the root) | `dir:src/shop` |
| file (non-Python) | `file:<path>` | `file:README.md` |
| module | dotted import name | `shop.service` |
| class, function, method | `<module>:<qualname>` | `shop.service:OrderService.place` |
| external package | `ext:<top-level name>` | `ext:requests` |

A module id is the name Python would import it by. For `src/shop/models.py` with
`src/shop/__init__.py` present, the id is `shop.models`. Loose scripts with no
package above them use their file name (`scripts/report.py` becomes `report`). If two
files would get the same id, both fall back to their path (`scripts.run`, `tools.run`).

### `attrs` by kind

| Kind | Key | Meaning |
|---|---|---|
| module | `is_package` | The file is an `__init__.py` |
| module | `parse_error` | The file could not be parsed; the scan carried on without it |
| module | `unresolved_imports` | Relative imports that point outside the project |
| module, function | `entry_point` | `main-guard`, `main-module`, `script:<name>`, `http`, `cli` or `task` |
| class | `bases` | Base classes as written, including external ones |
| class, function, method | `decorators` | Decorators as written |
| function, method | `async` | `async def` |
| module, class, function, method | `hash` | Fingerprint of the code. It ignores line numbers and docstrings, so moving code keeps it and editing code changes it. For modules, it covers the file's text |
| function, method | `shape` | Fingerprint of the code's structure with local names blanked out, for spotting the same logic under different names. Only for functions with three or more statements |
| module, class, function, method | `external_calls` | Calls into other packages, e.g. `requests.post` (up to 30) |
| module, class, function, method | `unresolved_calls` | How many calls could not be linked to anything |
| module, class, function, method | `unresolved_names` | The names of those calls (up to 40), so a diff can tell a removed call from one it could not link |
| file | `language`, `size` | Detected language and size in bytes |
| external | `stdlib` | Part of the Python standard library |

## Edges

| Field | Meaning |
|---|---|
| `source`, `target` | Node ids |
| `kind` | See below |
| `confidence` | Only on `calls`, `inherits` and `decorates` |
| `line` | First line in the source file where the relationship appears |
| `attrs` | `count` when it appears more than once; `names` and `type_only` on imports |

| Kind | From → to | Meaning |
|---|---|---|
| `contains` | directory → directory, file or module; module → top-level definitions; class → methods; function → nested definitions | Structure |
| `imports` | module → module or external | An import statement. `names` lists what was imported; `type_only` means every import of the target sits inside `if TYPE_CHECKING:` |
| `calls` | module, class, function or method → class, function or method | A call. Calling a class means creating an instance of it |
| `inherits` | class → class | Base class inside the project |
| `decorates` | function → class, function or method | The source decorates the target |

### Call confidence

Python is dynamic, so call edges are best effort, and each one says how it was found:

| Confidence | How it was found | Example |
|---|---|---|
| `resolved` | Names, imports, re-exports, `self.method()` and `super()` | `make_service()`, `self._sum()` |
| `inferred` | Through a type: an annotation, `x = Class()`, a factory's return annotation, or an attribute set in `__init__` | `order = Order(); order.total()` |
| `guessed` | The receiver's type is unknown, but exactly one method in the project has that name | `carrier.dispatch_parcel()` on an untyped parameter |

Calls that cannot be linked are not drawn. They are counted in the caller's
`unresolved_calls`, and calls into other packages are listed in `external_calls`.
Calls to builtins such as `print` or `list.append` are ignored.
