# fileops

[![CI](https://github.com/spash/fileops/actions/workflows/test.yml/badge.svg)](https://github.com/spash/fileops/actions/workflows/test.yml)

Atomic batch file operations for AI agent workflows.

---

## The problem

AI coding agents make file changes sequentially. Usually, this means one tool call per operation. A refactor touching N files requires O(N) round trips: read, confirm, write, confirm... Each conversational turn burns context window and API credits. Worse, a failure at step seven leaves the codebase in a partial state.

```text
agent → write auth.py       ✓
agent → write auth_test.py  ✓
agent → delete auth_old.py  ✓
agent → write routes.py     ✗  ← failure here
                                   auth.py is now inconsistent
                                   three files changed, one didn't
                                   rollback is manual
```

## The solution

Declare all operations in one spec. fileops executes them atomically: either every operation commits, or none of them do. By batching operations into a single payload, your agent achieves **O(1) turn complexity**. It costs the exact same 1 API call whether the agent refactors 1 file or 50.

```json
{
  "description": "refactor auth module",
  "operations": [
    { "type": "write",  "path": "src/auth.py",       "content": "..." },
    { "type": "write",  "path": "tests/auth_test.py", "content": "..." },
    { "type": "delete", "path": "src/auth_old.py" },
    { "type": "move",   "path": "src/routes.py", "destination": "src/api/routes.py" }
  ]
}
```

```
$ fileops run changes.json

✓ done  4/4 operations

  ✓  WRITE:  src/auth.py
  ✓  WRITE:  tests/auth_test.py
  ✓  DELETE: src/auth_old.py
  ✓  MOVE:   src/routes.py → src/api/routes.py
```

One call. One result. No partial states.

---

## Install

```bash
pip install fileops
```

Or from source:

```bash
git clone https://github.com/spash/tool-example
cd tool-example
pip install -e .
```

---

## CLI

### Run a spec

```bash
fileops run changes.json
fileops run changes.yaml
cat changes.json | fileops run -      # stdin
```

### Dry run: validate and diff, write nothing

```bash
fileops run changes.json --dry-run --diff
```

```diff
✓ done  4/4 operations (dry run)

  ✓  WRITE: src/auth.py

--- a/src/auth.py
+++ b/src/auth.py
@@ -1,8 +1,12 @@
-class Auth:
-    def login(self, user, password):
-        return db.check(user, password)
+class AuthService:
+    def __init__(self, db: Database) -> None:
+        self.db = db
+
+    def login(self, user: str, password: str) -> bool:
+        return self.db.check(user, password)
```

### Machine-readable output

```bash
fileops run changes.json --json
```

```json
{
  "success": true,
  "rolled_back": false,
  "operation_count": 4,
  "success_count": 4,
  "error": null,
  "results": [
    {
      "type": "write",
      "path": "src/auth.py",
      "success": true,
      "diff": "--- a/src/auth.py\n+++ b/src/auth.py\n...",
      "error": null
    }
  ]
}
```

---

## HTTP API

Start the server:

```bash
uvicorn fileops.api.main:app --reload
```

### POST /execute

Run a spec. Returns a BatchResult.

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d @changes.json
```

### POST /dry-run

Same as `/execute` but forces `dry_run: true`. Writes nothing.

```bash
curl -X POST http://localhost:8000/dry-run \
  -H "Content-Type: application/json" \
  -d @changes.json
```

### GET /health

```json
{ "status": "ok" }
```

Interactive docs at `http://localhost:8000/docs`.

---

## Python library

```python
from fileops.core import execute, load_spec
from fileops.core.models import BatchSpec, FileOperation, OperationType

# From a spec file
spec = load_spec("changes.json")
result = execute(spec)

# Inline
spec = BatchSpec(operations=[
    FileOperation(type=OperationType.WRITE, path="src/auth.py", content="..."),
    FileOperation(type=OperationType.DELETE, path="src/auth_old.py"),
])
result = execute(spec)

if result.success:
    print(f"{result.success_count} operations committed")
else:
    print(f"Failed — rolled back: {result.error}")
```

---

## How atomicity works

fileops uses POSIX rename semantics. The commit step is:

1. **Prepare:** write new content to a temp file in the same directory as the target (same filesystem). Back up any file that will be modified or deleted.
2. **Commit:** `os.replace(temp, target)` for each operation. On POSIX, `rename(2)` is atomic: the file either appears at the new path or it doesn't.
3. **Rollback:** if any operation fails, all committed operations are reversed in reverse order. Backed-up originals are restored. Newly created files are removed.

```
PREPARE         COMMIT              ROLLBACK (on failure)
───────         ──────              ─────────────────────
write → /tmp/.fileops_tmp_abc   os.replace(tmp, target)   os.replace(backup, target)
backup → /tmp/.fileops_bak_xyz                             os.replace(backup, target)
```

No temp files are left behind; cleanup runs on both success and failure paths.

> **Same-filesystem requirement:** fileops writes temp files into the same directory as the target to guarantee they share a filesystem with the destination. Cross-device moves fall back to a copy-then-delete.

---

## Spec format

### JSON

```json
{
  "description": "optional human-readable label",
  "dry_run": false,
  "operations": [
    { "type": "create", "path": "src/new_file.py",   "content": "# new\n" },
    { "type": "write",  "path": "src/existing.py",   "content": "# updated\n" },
    { "type": "delete", "path": "src/old_file.py" },
    { "type": "move",   "path": "src/a.py", "destination": "src/b.py" }
  ]
}
```

### YAML

```yaml
description: refactor auth module
dry_run: false
operations:
  - type: create
    path: src/new_file.py
    content: |
      # new
  - type: write
    path: src/existing.py
    content: |
      # updated
  - type: delete
    path: src/old_file.py
  - type: move
    path: src/a.py
    destination: src/b.py
```

### Operation types

| Type     | Required fields        | Description                        |
|----------|------------------------|------------------------------------|
| `create` | `path`, `content`      | Create a new file                  |
| `write`  | `path`, `content`      | Overwrite an existing file         |
| `delete` | `path`                 | Delete a file                      |
| `move`   | `path`, `destination`  | Move or rename a file              |

---

## Integrating with an agent

fileops ships a JSON/YAML interface so agents can generate specs without invoking a Python SDK. Point your agent at the HTTP API or have it write a spec file and shell out to the CLI.

**Example: structured output from Claude**

```python
import anthropic, json
from fileops.core import execute, load_spec

client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    system="""You are a coding agent. When asked to modify files, respond ONLY
with a valid fileops JSON spec. No explanation, no markdown fences.""",
    messages=[{
        "role": "user",
        "content": f"Refactor the auth module to use dependency injection. "
                   f"Current code:\n\n{current_code}"
    }]
)

spec = load_spec(json.loads(response.content[0].text))
result = execute(spec)

print(f"{'✓' if result.success else '✗'}  {result.success_count}/{result.operation_count} operations")
```

**Example: pipe through the CLI**

```bash
claude --output-format json "Refactor auth.py to use DI" \
  | jq '.content' \
  | fileops run -
```

---

## Development

```bash
git clone https://github.com/spash/tool-example
cd tool-example
pip install -e ".[dev]"
python -m unittest discover -s tests -v
ruff check .
```

### Project layout

```
fileops/
  core/
    models.py     ← typed contracts: BatchSpec, FileOperation, BatchResult
    executor.py   ← atomic execution engine
    differ.py     ← unified diff generation (stdlib only)
    parser.py     ← JSON/YAML → BatchSpec
  cli/
    main.py       ← Click CLI
  api/
    main.py       ← FastAPI server
  tests/
    test_executor.py   ← 33 tests: all op types, rollback, dry-run, cleanup
    test_parser.py
```

Core has no external dependencies beyond the Python stdlib. Click, FastAPI, and PyYAML are optional; the library works without them.

---
Extracted from an internal project. Core logic is unchanged; the original included a UI diff previewer that didn’t make sense as a standalone tool.

---
## License

MIT
