# Tools

The `tools/` subpackage implements the file-manipulation and shell tools that the agent can invoke. Each tool is a plain Python function (or async function for `Bash`) registered with coreouto via `tools/registry.py`.

```
src/miniouto/tools/
├── __init__.py
├── _normalize.py     # smart-quote/dash/NBSP/zero-width normalization (fuzzy edit)
├── bash.py           # async bash(command, *, timeout_seconds, cwd, env)
├── delete.py         # delete(file_path)
├── edit.py           # edit(file_path, edits) — batch search/replace
├── media.py          # load_image/load_video/load_audio — read media bytes (pure stdlib)
├── write.py          # write(file_path, content) — refuse overwrite
└── registry.py       # register_all() — wires tools into coreouto
```

**Layer rules:**
- `bash.py`, `delete.py`, `edit.py`, `media.py`, `write.py` are **pure stdlib** (no coreouto dependency). They are the only tools layer code that touches the filesystem outside `storage/`.
- `registry.py` is the **only** tools file that imports coreouto. It defines the JSON schemas, descriptions, the registration glue, **and** the construction of multimodal `ContentBlock`s for the media tools (`media.py` returns raw `LoadedMedia` records; `registry.py` wraps them into `co.ImageBlock` / `co.VideoBlock` / `co.AudioBlock`).
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

This dict is **computed but never actually passed to coreouto** — see "A note on schemas" under `tools/registry.py` below. The handler's Python type hints + the `_write_description` string are what reach the model. The dict is reproduced for reference:

```json
{
  "type": "object",
  "properties": {
    "file_path": {"type": "string", "description": "Absolute path of the file to create."},
    "content":   {"type": "string", "description": "Full file content to write."}
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
5. Applies all replacements in a single pass against the original offsets (insertion order, not sorted — `_check_no_overlaps` sorts internally for its check, but the apply loop iterates `spans` in edit order; order is irrelevant for correctness because spans are non-overlapping and use original offsets).
6. Writes via `Path.write_text` (not atomic — in contrast to `write.py`'s `_atomic_write_text`).
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
- **`_summary(spans, file_path, new_content) -> str`** — `"Applied N edit(s) to <path>."` (`edit`/`edits` pluralized correctly) followed by one line per edit with the new line number and an 80-char preview of the **first line** of `newText`.

### `class EditError(Exception)`

```python
class EditError(Exception):
    def __init__(self, message: str, *, file_path: str | None = None) -> None:
        super().__init__(message)
        self.file_path = file_path
```

The `file_path` keyword argument is only set by the early path-validation failures (file-not-found, is-a-directory). Location / overlap / no-match errors embed the path in the message string but do **not** set the `file_path` attribute — by default it is `None`.

### JSON schema (`_edit_schema`)

Computed but **not passed to coreouto** at registration time (see "A note on schemas" below). Reproduced for reference — the actual schema **does** include `description` fields on every property:

```json
{
  "type": "object",
  "properties": {
    "file_path": {"type": "string", "description": "Absolute path of the file to edit."},
    "edits": {
      "type": "array",
      "description": "List of {oldText, newText} pairs to apply in one batch.",
      "items": {
        "type": "object",
        "properties": {
          "oldText": {"type": "string", "description": "Exact text to replace."},
          "newText": {"type": "string", "description": "Replacement text."}
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

Computed but **not passed to coreouto** (see "A note on schemas" below). Reproduced for reference:

```json
{
  "type": "object",
  "properties": {
    "file_path": {"type": "string", "description": "Absolute path to delete."}
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
- Formats output (via `_format_output`) as:

  ```
  <stdout>

  [stderr]
  <stderr>

  [exit RC in T.TTs, cwd=<INVOCATION_CWD>]
  ```

  Note: the exit line **always** reports `INVOCATION_CWD`, **not** the actual `cwd`/`workdir` argument — `_format_output` hardcodes it. (This is arguably a small code bug; the model sees the invocation directory regardless of any `--cwd` override.)

- Truncates the formatted output to `MAX_OUTPUT_BYTES` (30 KB) using UTF-8-safe byte truncation with a `<NOTE>` suffix.
- `cwd` defaults to `INVOCATION_CWD` (the user's cwd at miniouto invocation).
- `env` is **merged on top of** `os.environ` — existing env vars are preserved unless explicitly overridden. Note: the underlying `bash()` accepts `env`, but the model-facing handler `_bash_handler` does **not** expose it (see schemas below), so the LLM cannot set custom env vars.

Raises `BashError` on empty command, spawn failure, or timeout.

### `class BashError(Exception)`

Raised by `bash` on any failure mode.

### JSON schema (`_bash_schema`)

Computed but **not passed to coreouto** (see "A note on schemas" below). Reproduced for reference. Note: there is **no** `env` property and **no** `"default": 60` key in the actual schema — the default is mentioned only inside the description text:

```json
{
  "type": "object",
  "properties": {
    "command":         {"type": "string", "description": "Shell command to execute."},
    "timeout_seconds": {"type": "integer", "description": "Max seconds to wait (default 60, max 600).",
                        "minimum": 1, "maximum": 600},
    "cwd":             {"type": "string", "description": "Override working directory (default: process cwd)."}
  },
  "required": ["command"]
}
```

The handler `_bash_handler(command, timeout_seconds=60, cwd=None)` likewise has no `env` parameter, so even if a model sent `env` it would not be forwarded.

### Why `bash` is the only async tool

`asyncio.create_subprocess_shell` integrates cleanly with the TUI's event loop. The other tools are pure file I/O — running them with `asyncio.to_thread` from the TUI works fine. Keeping `bash` async avoids spawning an extra thread for every shell command.

---

## `tools/media.py`

Read image / video / audio files from disk so the LLM can perceive them directly. Unlike the text tools, these do **not** return a string — they hand back a `LoadedMedia` record that `registry.py` wraps into coreouto `ContentBlock`s (`ImageBlock` / `VideoBlock` / `AudioBlock`). coreouto then forwards the raw bytes to the provider as a multimodal tool result, so the model receives the actual pixels / frames / waveform rather than a text description.

**Layer rule**: `media.py` is pure stdlib. The `coreouto` import and `ContentBlock` construction live in `registry.py` (see "Handlers" under `tools/registry.py` below).

### Constants

```python
MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 20 MB
MAX_VIDEO_BYTES = 50 * 1024 * 1024   # 50 MB
MAX_AUDIO_BYTES = 25 * 1024 * 1024   # 25 MB

_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
_VIDEO_MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
_AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
```

The size caps are deliberately conservative — below the provider hard limits (Anthropic: image 30 MB) so a single tool call can never trip the provider's request-size rejection. Multimodal payloads are uploaded verbatim; a 200 MB video would blow the request budget. When a file exceeds the cap, the tool raises `MediaViewError` with a redirect to Bash-based downsampling (`ffmpeg`, `sox`, ImageMagick `convert`).

The MIME tables are scoped to the formats coreouto's block types accept (see [coreouto `tools.md` — Multimodal tool results](https://github.com/llaa33219/coreouto/blob/main/docs/tools.md#content-block-types)). Adding an extension the active provider does not understand surfaces as a provider-side `ValueError` at call time.

### `@dataclass LoadedMedia`

```python
@dataclass
class LoadedMedia:
    path: Path
    data: bytes
    mime_type: str
    kind: str   # "image" | "video" | "audio"
```

`kind` is carried separately from `mime_type` so `registry.py` can dispatch to the right block constructor without re-parsing the MIME string.

### `load_image(file_path: str) -> LoadedMedia`

`load_video(file_path: str) -> LoadedMedia`

`load_audio(file_path: str) -> LoadedMedia`

Each delegates to the shared `_load(file_path, kind, mime_table, max_bytes)`. Behavior:

1. Rejects empty / non-string `file_path` (raises `MediaViewError`).
2. Resolves relative paths against `paths_runtime.INVOCATION_CWD` (same rule as `delete`).
3. Raises `MediaViewError` if the path is missing or is a directory.
4. Sniffs the MIME type from the lowercased suffix; raises `MediaViewError` listing the supported extensions if the suffix is unrecognized.
5. Enforces the kind-specific byte cap (`stat().st_size`); raises `MediaViewError` with a downsample hint on overflow.
6. Rejects empty (0-byte) files.
7. Reads the full file into memory via `read_bytes()` and returns `LoadedMedia`.

### `class MediaViewError(Exception)`

Raised by all three loaders on every failure mode above. Carries a human-readable message; unlike `EditError` it has no extra attributes — the path and kind are embedded in the message text.

### Provider support (important)

These tools only produce useful results on **multimodal-capable** providers. Per coreouto's matrix:

| Provider | image | video | audio |
|---|---|---|---|
| Anthropic | yes | yes | yes |
| Google (new SDK) | yes | yes | yes |
| OpenAI Responses API | yes | **no** (`ValueError`) | **no** (`ValueError`) |
| OpenAI Chat Completions | **no** (`ValueError`) | **no** (`ValueError`) | **no** (`ValueError`) |

On a non-multimodal provider, the tool call succeeds (the loader runs, the blocks are built) but the **next** LLM call raises `ValueError` from the provider's serialization layer. The tool descriptions warn the model about this inline. If your workflow needs media on OpenAI, switch the preset's provider to `openai-response` (enables image + document) or use Anthropic / Google.

---

## `tools/_normalize.py`

Fuzzy-matching helpers for the Edit tool's fallback path.

### Constants

```python
SMART_QUOTE_MAP  = str.maketrans(...)  # ' ' ' " " → ' " etc.
DASH_MAP         = str.maketrans(...)  # various dashes (– — ― ‐ ‑ ‒ − etc.) → -
NBSP             = "\u00a0"
ZERO_WIDTH       = ("\u200b", "\u200c", "\u200d", "\ufeff")
BOM              = "\ufeff"            # also a member of ZERO_WIDTH
```

### `normalize_for_matching(s) -> str`

Applies, in order:
1. CRLF → LF (and lone `\r` → `\n`).
2. Smart-quote translate (`SMART_QUOTE_MAP`).
3. Dash translate (`DASH_MAP`).
4. NBSP → space.
5. Strip zero-width characters (also strips BOM, which is in the `ZERO_WIDTH` tuple).
6. NFKC normalization (handles composition, compatibility decomposition).
7. Right-strip every line.

Used by `_locate_unique_fuzzy` to compare two strings after both have been normalized.

### `first_diff_index(a, b) -> int`

Returns the index of the first differing character, or `min(len(a), len(b))` if no difference is found in the common prefix. (So if `a` is a prefix of `b`, this equals `len(a)`; if `b` is a prefix of `a`, it equals `len(b)`.)

### `find_occurrences(haystack, needle) -> list[int]`

Returns all start indices where `needle` occurs in `haystack`. Returns `[]` for an empty needle.

---

## `tools/registry.py`

Wires the four file/bash tools into coreouto's tool registry.

### `register_all()`

Idempotent: calls `_register_if_missing(name, handler, schema, description)` for `Write`, `Edit`, `Delete`, `Bash`, `Image`, `Video`, `Audio`. (The `call_subagent` tool is registered separately in `core.runtime.build_runtime` because it needs the subagent config to be built first.)

### `_register_if_missing(name, handler, schema, description)`

Skips if `co.get_tool(name)` is already set; otherwise calls `co.register_tool(name, description=description)(handler)` — **the `schema` parameter is accepted but silently discarded**. This is what makes repeated `build_runtime` calls safe in TUI mode.

#### A note on schemas (dead code)

The `_xxx_schema()` functions are invoked at registration time (`_register_if_missing("Write", _write_handler, _write_schema(), _write_description())`), but their return values are never forwarded to `coreouto.register_tool`. Only the handler's Python type hints and the `description` string reach the model. The schema dicts in this file are effectively documentation-only. Do not rely on them affecting model behavior; if you need the model to see a parameter restriction, encode it in the description text.

### Handlers (private)

| Tool | Handler | Signature |
|---|---|---|
| `Write` | `_write_handler(file_path, content) -> str` | sync |
| `Edit` | `_edit_handler(file_path, edits) -> str` | sync |
| `Delete` | `_delete_handler(file_path) -> str` | sync |
| `Bash` | `async _bash_handler(command, timeout_seconds=60, cwd=None) -> str` | async (no `env` — the handler signature does not expose it even though `bash()` does) |
| `Image` | `_image_handler(file_path) -> list[co.ContentBlock]` | sync, **multimodal** — returns `[TextBlock, ImageBlock]` |
| `Video` | `_video_handler(file_path) -> list[co.ContentBlock]` | sync, **multimodal** — returns `[TextBlock, VideoBlock]` |
| `Audio` | `_audio_handler(file_path) -> list[co.ContentBlock]` | sync, **multimodal** — returns `[TextBlock, AudioBlock]` |

The media handlers are the **only** handlers in this file that return something other than `str`. They delegate the file read to `tools.media.load_*` (which returns a `LoadedMedia`), then build a two-element block list: a `TextBlock` caption (path + byte count + MIME) and the binary block. coreouto forwards the list to the provider as a multimodal tool result. Do **not** refactor these to return `str` — that would discard the media payload and silently degrade the tools to "the file exists" no-ops. Contract: [coreouto `tools.md` — Multimodal tool results](https://github.com/llaa33219/coreouto/blob/main/docs/tools.md#multimodal-tool-results).

### Descriptions (what the LLM actually sees)

Each description includes the tool's restrictions inline. Verbatim from `registry.py`:

| Tool | Description (verbatim) |
|---|---|
| `Write` | "Create a new file with the given content. Refuses to overwrite an existing file — use the Edit tool for changes to existing files. Parent directories are created automatically. Pass an absolute path, or a path relative to the directory miniouto was invoked from. Content is capped at 50,000 characters: large inline content is likely to be silently truncated at the model layer, producing a partial file. For large or generated content, compose it with Bash (heredoc, printf, seq loop, or a short Python one-liner) and have Bash write the file directly." |
| `Edit` | "Apply one or more search/replace edits to a file. Each edit has oldText (the exact string to find) and newText (its replacement). Multiple edits in one call all match against the original file; they cannot overlap. oldText must be unique within the file unless more context is provided. Pass an absolute path, or a path relative to the directory miniouto was invoked from." |
| `Delete` | "Delete a file or an empty directory. Refuses to delete a non-empty directory — use Bash with `rm -rf` if you really mean it. Pass an absolute path, or a path relative to the directory miniouto was invoked from." |
| `Bash` | "Run a shell command. Captures stdout and stderr; exits with the command's exit code. Default timeout 60s, max 600s. Output >30KB is truncated with a note. Default cwd is the directory miniouto was invoked from. Use this for `git`, `grep`, `find`, `ls`, `cat`, `pytest`, package managers, etc." |
| `Image` | "View an image file and return it to the model so it can actually be seen. Supports PNG, JPEG, GIF, WebP. Capped at 20 MB. Pass an absolute path, or a path relative to the directory miniouto was invoked from. The file's raw bytes are uploaded to the provider as an image content block — the model receives the pixels, not a text description. For unsupported formats or oversized files, convert first with Bash (e.g. ImageMagick `convert`, Pillow)." |
| `Video` | "View a video file and return it to the model so it can actually be perceived. Supports MP4, MOV, WebM. Capped at 50 MB. Pass an absolute path, or a path relative to the directory miniouto was invoked from. The file's raw bytes are uploaded to the provider as a video content block. For unsupported formats or oversized files, downsample first with Bash (e.g. ffmpeg)." |
| `Audio` | "View an audio file and return it to the model so it can actually be heard. Supports WAV, MP3. Capped at 25 MB. Pass an absolute path, or a path relative to the directory miniouto was invoked from. The file's raw bytes are uploaded to the provider as an audio content block. For unsupported formats or oversized files, downsample first with Bash (e.g. sox, ffmpeg)." |

> **Why no provider names in the descriptions**: the agent cannot introspect which provider it is running on, so telling it "OpenAI Chat Completions rejects video" is not actionable — it cannot classify itself. If a provider rejects a multimodal block, the `ValueError` surfaces at call time and that error message is the teaching signal. The full provider support matrix for human operators lives in the `tools/media.py` section below.

---

## Path resolution

Every tool resolves relative paths against `paths_runtime.INVOCATION_CWD` (the cwd captured at miniouto import time). This is the cwd the user invoked miniouto from — not the cwd at tool-call time, which can drift if the agent's earlier `Bash` calls did `cd`.

If you need the agent to operate relative to a different directory, pass an absolute path or have a `Bash` call do the `cd` first.

---

## Adding a new tool

1. Create `src/miniouto/tools/<name>.py` with a single function `<name>(**kwargs) -> str` (or `async def`). Keep it **pure stdlib** — no `coreouto` import. If the tool needs to return media (image/video/audio bytes), return a plain data structure (like `media.py`'s `LoadedMedia`) and let `registry.py` build the `co.ContentBlock`s. See `tools/media.py` for the pattern.
2. Add the function's description, schema, and handler to `tools/registry.py`. Add `_<name>_handler`, `_<name>_schema`, `_<name>_description` and wire them via `_register_if_missing` inside `register_all`. (Note: per "A note on schemas" above, the schema dict is currently discarded at registration — the description string is what reaches the model.) **For multimodal tools**, the handler returns `list[co.ContentBlock]` instead of `str` — see the `Image` / `Video` / `Audio` handlers for the exact shape.
3. Add the name to `core/runtime.ALL_TOOLS` (this controls which tools are visible to both outo and subagent presets — both `register_agent_preset("outo", tools=ALL_TOOLS, …)` and `register_agent_preset("subagent", tools=ALL_TOOLS, …)` reference it).
4. If the tool should only be visible to outo (not subagent), create separate tool lists and edit the `tools=` argument in each `register_agent_preset` call. **Do not confuse this with `_resolve_both_styles`** — that function only resolves the style *prompts*, not the tool lists.
5. Add the tool name to `_LOGGABLE_TOOL_NAMES` and the tool-name set in `_make_tool_call_dispatcher` (plus a branch in `_short_arg_summary`) in `core/chat.py`, so loop events and failure diagnostics render the new tool nicely. (The media tools `Image` / `Video` / `Audio` are examples of this wiring.)
6. Update `default_style/*.md` if the tool's name or behavior should be documented to the model.
7. Add a `Write`/`Edit`/`Delete`-style test for the new tool's edge cases (none exist yet, so this is a chance to start the test suite).
8. If the new tool returns multimodal content, note that provider support varies (see the matrix in `tools/media.py` below). Do **not** put provider names in the tool description or style prompts — the agent cannot introspect its own provider, so such hints are unactionable. Let provider rejections surface naturally as `ValueError` at call time; that error is the teaching signal. Document the matrix here in `docs/tools.md` for human operators instead.
