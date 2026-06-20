# Tools

The `tools/` subpackage implements the file-manipulation and shell tools that the agent can invoke. Each tool is a plain Python function (or async function for `Bash`) registered with coreouto via `tools/registry.py`.

```
src/miniouto/tools/
├── __init__.py
├── _normalize.py     # smart-quote/dash/NBSP/zero-width normalization (fuzzy edit)
├── bash.py           # async bash(command, *, timeout_seconds, cwd, env)
├── delete.py         # delete(file_path)
├── edit.py           # edit(file_path, edits) — batch search/replace
├── write.py          # write(file_path, content) — refuse overwrite
└── registry.py       # register_all() — wires tools into coreouto
```

**Layer rules:**
- `bash.py`, `delete.py`, `edit.py`, `write.py` are **pure stdlib** (no coreouto dependency). They are the only tools layer code that touches the filesystem outside `storage/`.
- `registry.py` is the **only** tools file that imports coreouto. It defines the JSON schemas, descriptions, and the registration glue.
- `_normalize.py` is a helper module, not a tool — it's re-exported via `from ._normalize import normalize_for_matching`.

---

## `tools/write.py`

### Constants

```python
MAX_CONTENT_CHARS = 50_000
```

The Write tool refuses longer inline content (the model layer may truncate mid-line).

### `write(file_path: str, content: str, *, encoding: str = "utf-8") -> str`

Behavior:
- Refuses non-string `content` (raises `WriteError`).
- Refuses content longer than `MAX_CONTENT_CHARS`.
- Refuses to **overwrite** an existing file (raises `WriteError`). The intended workflow is `Write` to create, `Edit` to modify.
- Auto-creates parent directories.
- Writes atomically via a sibling temp file + `os.replace` (via `_atomic_write_text` helper using `tempfile.mkstemp` in the same directory).

Returns `"Created <path> (N chars)."`

### `_atomic_write_text(path, content, *, encoding)`

Internal helper. Uses `tempfile.mkstemp` in the same directory to guarantee `os.replace` is a same-filesystem rename. On any error after writing the temp file, the temp is cleaned up before re-raising.

### `class WriteError(Exception)`

Raised by `write` on any of the failure modes above.

### JSON schema (`_write_schema`)

```json
{
  "type": "object",
  "properties": {
    "file_path": {"type": "string", "description": "Absolute path to create."},
    "content":   {"type": "string", "description": "UTF-8 file contents. Max 50,000 chars."}
  },
  "required": ["file_path", "content"]
}
```

---

## `tools/edit.py`

Batch search/replace with exact-match priority and a fuzzy fallback.

### `edit(file_path: str, edits: list[dict[str, str]], *, encoding: str = "utf-8") -> str`

`edits` is a list of `{oldText: str, newText: str}` objects. All edits are located against the **original** content (no chaining), then applied in a single pass.

Behavior:
1. Validates args (`_validate_edit`).
2. Reads file content.
3. For each edit, locates `oldText` (exact first, fuzzy fallback).
4. Checks for overlaps between spans (`_check_no_overlaps`).
5. Applies all replacements in a single pass (sorted by start position).
6. Writes atomically via `Path.write_text`.
7. Returns a human-readable summary via `_summary`.

### Six enforced rules

1. **Exact match** priority — fuzzy is only used as a fallback if exact fails.
2. **Uniqueness** — multiple occurrences raise `EditError` listing the line numbers of every occurrence.
3. **All edits located against the original content** — no chaining (`edit A then edit B` doesn't see A's changes).
4. **No overlaps** — sorted-span check raises on any overlap.
5. **Reject empty / no-op edits** — `_validate_edit` rejects empty `oldText`, missing keys, non-string values, and identical `oldText`/`newText`.
6. **Errors carry line numbers + how-to-fix** — `_no_match_message` includes the first-line snippet of `oldText` for diagnostic feedback.

### Helpers

- **`_validate_edit(index, edit)`** — rejects non-dict, missing keys, non-string values, empty `oldText`, identical `oldText`/`newText`.
- **`_locate_unique(content, old_text, *, edit_index, file_path) -> (start, end)`** — exact-match path; raises `EditError` listing the line numbers of every occurrence on ambiguity.
- **`_locate_unique_fuzzy(content, old_text, *, edit_index, file_path) -> (start, end)`** — normalizes both sides via `normalize_for_matching`, retries.
- **`_check_no_overlaps(spans, file_path)`** — sorts spans by start, raises on any overlap.
- **`_lines_for_indices(content, indices, length) -> list[int]`** — converts byte offsets to 1-based line numbers.
- **`_no_match_message(content, old_text, edit_index, file_path, fuzzy=False)`** — composes the "not found" error with a short snippet of the first line of `oldText`.
- **`_summary(spans, file_path, new_content) -> str`** — `"Applied N edit(s) to <path>."` followed by one line per edit with the new line number and a 80-char preview of `newText`.

### `class EditError(Exception)`

```python
class EditError(Exception):
    file_path: str | None = None
```

The `file_path` attribute is set when the error is per-file (e.g. during location) — otherwise it may be `None`.

### JSON schema (`_edit_schema`)

```json
{
  "type": "object",
  "properties": {
    "file_path": {"type": "string"},
    "edits": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "oldText": {"type": "string"},
          "newText": {"type": "string"}
        },
        "required": ["oldText", "newText"]
      }
    }
  },
  "required": ["file_path", "edits"]
}
```

---

## `tools/delete.py`

### `delete(file_path: str) -> str`

Behavior:
- Resolves relative path against `paths_runtime.INVOCATION_CWD`.
- Deletes files with `unlink()`.
- Deletes **empty** directories with `rmdir()`.
- Refuses non-empty directories (raises `DeleteError`).
- Raises `DeleteError` if path is missing.

Returns a short confirmation message.

### `class DeleteError(Exception)`

Raised by `delete` on any failure mode.

### JSON schema (`_delete_schema`)

```json
{
  "type": "object",
  "properties": {
    "file_path": {"type": "string"}
  },
  "required": ["file_path"]
}
```

---

## `tools/bash.py`

Async shell tool.

### Constants

```python
MAX_OUTPUT_BYTES = 30_000
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 600
TRUNCATION_NOTE = "<NOTE>Output was truncated to {max} bytes. ...</NOTE>"
```

### `async bash(command: str, *, timeout_seconds: int = 60, cwd: str | None = None, env: dict[str, str] | None = None) -> str`

Behavior:
- Spawns `asyncio.create_subprocess_shell` with `stdout=PIPE, stderr=PIPE`.
- Captures stdout + stderr.
- On timeout: kills the process and raises `BashError`.
- Formats output as:

  ```
  <stdout>
  [stderr]
  <stderr>
  [exit RC in T.TTs, cwd=<cwd>]
  ```

- Truncates the formatted output to `MAX_OUTPUT_BYTES` (30 KB) using UTF-8-safe byte truncation with a `<NOTE>` suffix.
- `cwd` defaults to `INVOCATION_CWD` (the user's cwd at miniouto invocation).
- `env` is **merged on top of** `os.environ` — existing env vars are preserved unless explicitly overridden.

Raises `BashError` on empty command, spawn failure, or timeout.

### `class BashError(Exception)`

Raised by `bash` on any failure mode.

### JSON schema (`_bash_schema`)

```json
{
  "type": "object",
  "properties": {
    "command":         {"type": "string"},
    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60},
    "cwd":             {"type": "string"},
    "env":             {"type": "object", "additionalProperties": {"type": "string"}}
  },
  "required": ["command"]
}
```

### Why `bash` is the only async tool

`asyncio.create_subprocess_shell` integrates cleanly with the TUI's event loop. The other tools are pure file I/O — running them with `asyncio.to_thread` from the TUI works fine. Keeping `bash` async avoids spawning an extra thread for every shell command.

---

## `tools/_normalize.py`

Fuzzy-matching helpers for the Edit tool's fallback path.

### Constants

```python
SMART_QUOTE_MAP  = str.maketrans(...)  # ' ' ' " " → ' " etc.
DASH_MAP         = str.maketrans(...)  # – — ― ~ → -
NBSP             = "\u00a0"
ZERO_WIDTH       = ("\u200b", "\u200c", "\u200d", "\ufeff")
BOM              = "\ufeff"
```

### `normalize_for_matching(s) -> str`

Applies, in order:
1. CRLF → LF
2. Smart-quote translate (`SMART_QUOTE_MAP`)
3. Dash translate (`DASH_MAP`)
4. NBSP → space
5. Strip zero-width characters
6. NFKC normalization (handles composition, compatibility decomposition)
7. Right-strip every line

Used by `_locate_unique_fuzzy` to compare two strings after both have been normalized.

### `first_diff_index(a, b) -> int`

Returns the index of the first differing character, or `len(a)` if `a` is a prefix of `b`.

### `find_occurrences(haystack, needle) -> list[int]`

Returns all start indices where `needle` occurs in `haystack`. Returns `[]` for an empty needle.

---

## `tools/registry.py`

Wires the four file/bash tools into coreouto's tool registry.

### `register_all()`

Idempotent: calls `_register_if_missing(name, handler, schema, description)` for `Write`, `Edit`, `Delete`, `Bash`. (The `call_subagent` tool is registered separately in `core.runtime.build_runtime` because it needs the subagent config to be built first.)

### `_register_if_missing(name, handler, schema, description)`

Skips if `co.get_tool(name)` is already set; otherwise calls `co.register_tool(name, description=description)(handler)`. This is what makes repeated `build_runtime` calls safe in TUI mode.

### Handlers (private)

| Tool | Handler | Signature |
|---|---|---|
| `Write` | `_write_handler(file_path, content) -> str` | sync |
| `Edit` | `_edit_handler(file_path, edits) -> str` | sync |
| `Delete` | `_delete_handler(file_path) -> str` | sync |
| `Bash` | `async _bash_handler(command, timeout_seconds=60, cwd=None) -> str` | async |

### Schemas + descriptions

For each tool, a `_<name>_schema` (dict passed to `register_tool`) and `_<name>_description` (str) are defined. Each description includes the tool's restrictions inline so the LLM sees them in its system prompt:

| Tool | Description includes |
|---|---|
| `Write` | "Creates a new file. Refuses to overwrite existing files. Max 50,000 chars inline." |
| `Edit` | "Applies one or more search/replace operations to an existing file. Each oldText must match exactly once (or after normalization if needed). All edits are applied in a single pass." |
| `Delete` | "Removes a file or empty directory. Refuses non-empty directories." |
| `Bash` | "Runs a shell command asynchronously. Captures stdout and stderr. Default timeout 60s, max 600s. Output truncated to 30 KB." |

---

## Path resolution

Every tool resolves relative paths against `paths_runtime.INVOCATION_CWD` (the cwd captured at miniouto import time). This is the cwd the user invoked miniouto from — not the cwd at tool-call time, which can drift if the agent's earlier `Bash` calls did `cd`.

If you need the agent to operate relative to a different directory, pass an absolute path or have a `Bash` call do the `cd` first.

---

## Adding a new tool

1. Create `src/miniouto/tools/<name>.py` with a single function `<name>(**kwargs) -> str` (or `async def`).
2. Add the function's description, schema, and handler to `tools/registry.py`.
3. Add `_<name>_handler`, `_<name>_schema`, `_<name>_description` to `registry.py`.
4. Add the name to `core/runtime.ALL_TOOLS` (this controls which tools are visible to both outo and subagent presets).
5. If the tool should only be visible to outo (not subagent), add it to a new list and adjust `_resolve_both_styles` in `core/runtime.py` accordingly.
6. Update `default_style/*.md` if the tool's name or behavior should be documented to the model.
7. Add a `Write`/`Edit`/`Delete`-style test for the new tool's edge cases (none exist yet, so this is a chance to start the test suite).
