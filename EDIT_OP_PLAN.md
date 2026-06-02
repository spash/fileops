# FileOps: add an `EDIT` operation

Planned for later tonight / tomorrow. Goal: let fileops express a small, surgical change without restating an entire file.

## Why

Today fileops only has `WRITE` (full file content) and `DELETE`. Any change, even a one-line insertion, requires emitting the whole file. That's expensive (tokens, diff noise) and discourages using fileops for exactly the multi-file tasks it's best at. Result: we fall back to `perl`/manual edits, which work but give up fileops' atomic all-or-nothing rollback.

Real example that triggered this: wiring one nav link + one footer `<li>` into 5 HTML files. Should have been one atomic batch of tiny edits; instead it was 5 full rewrites (too costly) or 9 separate `perl` calls (cheap, no rollback).

## The fix: `EDIT` (string replacement)

Mirror the semantics of the editor's built-in Edit tool.

New `OperationType.EDIT` with these `FileOperation` fields:

| field | type | notes |
|-------|------|-------|
| `type` | `OperationType.EDIT` | |
| `path` | str | file must exist |
| `old_string` | str | exact text to find |
| `new_string` | str | replacement |
| `replace_all` | bool = False | replace every occurrence |

### Validation rules (these are where atomic rollback pays off)

- `path` does not exist → fail the batch.
- `old_string` not found → fail the batch.
- `old_string == new_string` → fail (no-op, likely a mistake).
- `old_string` found more than once and `replace_all` is False → fail (ambiguous), same as the Edit tool.
- All failures abort the whole `BatchSpec` and roll back; nothing partial commits.

### Example

```python
from fileops.core import execute
from fileops.core.models import BatchSpec, FileOperation, OperationType

spec = BatchSpec(operations=[
    FileOperation(type=OperationType.WRITE, path="work/idea-network.html", content="..."),
    FileOperation(
        type=OperationType.EDIT,
        path="index.html",
        old_string='<a href="work/moodiboard.html">MoodiBoard</a>\n      </div>',
        new_string='<a href="work/moodiboard.html">MoodiBoard</a>\n        <a href="work/idea-network.html">Idea Network</a>\n      </div>',
    ),
    FileOperation(type=OperationType.EDIT, path="about.html",  old_string="...", new_string="...", replace_all=False),
])
result = execute(spec)
```

## Optional: `INSERT` (anchor-based) — only if we want it

Pure insertions read more clearly than an `EDIT` that repeats the anchor in both strings.

| field | type | notes |
|-------|------|-------|
| `type` | `OperationType.INSERT` | |
| `path` | str | |
| `anchor` | str | text to locate |
| `position` | `"before" \| "after"` | |
| `content` | str | inserted verbatim |

Same not-found / ambiguous-match failure rules as `EDIT`. Strictly optional; `EDIT` alone covers every case.

## Implementation touchpoints

1. `core/models.py` — add `EDIT` (and maybe `INSERT`) to the `OperationType` enum; add the new optional fields to `FileOperation`; validate that the right fields are present per type.
2. `core/` execute logic — add a handler for `EDIT`: read file, run the find/replace with the count checks above, stage the new content into the same atomic write/rollback path that `WRITE` already uses.
3. The CLI (`fileops run changes.json`) — ensure the JSON schema accepts `old_string` / `new_string` / `replace_all` (and `anchor` / `position` if INSERT lands).

## Tests to add

- EDIT happy path (single match).
- EDIT `replace_all=True` (multiple matches replaced).
- EDIT ambiguous (>1 match, `replace_all=False`) → batch fails, no file changed.
- EDIT not-found → batch fails, no file changed.
- EDIT no-op (`old == new`) → fails.
- Mixed batch (WRITE + EDIT + DELETE) where one EDIT fails → entire batch rolls back, all files unchanged.
- (If INSERT) before/after an anchor; not-found; ambiguous.

## Out of scope for this pass

- Regex / multi-anchor matching.
- Line-number-based patching.
- Fuzzy matching.
Keep it exact-string only, matching the editor's Edit semantics, so behavior is predictable.
