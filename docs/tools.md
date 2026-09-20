# Tools

The `tools/` subpackage implements the tools the agent can invoke: one shell tool (`Bash`) and three media viewers (`Image` / `Video` / `Audio`). Each tool is a plain Python function (or async function for `Bash`) registered with coreouto via `tools/registry.py`.

```
src/miniouto/tools/
├── __init__.py
├── bash.py           # async bash(command, *, cwd, env)
├── media.py          # load_image/load_video/load_audio — read media bytes (pure stdlib)
└── registry.py       # register_all() — wires tools into coreouto
```

**Layer rules:**
- `bash.py` and `media.py` are **pure stdlib** (no coreouto dependency). They are the only tools layer code that touches the filesystem outside `storage/`.
- `registry.py` is the **only** tools file that imports coreouto. It defines the JSON schemas, descriptions, the registration glue, **and** the construction of multimodal `ContentBlock`s for the media tools (`media.py` returns raw `LoadedMedia` records; `registry.py` wraps them into `co.ImageBlock` / `co.VideoBlock` / `co.AudioBlock`).

## Why Bash is the only file tool

miniouto deliberately has **no dedicated Write/Edit/Delete tools**. File manipulation goes through `Bash` (`cat`, `grep`, `sed -i`, `rm`, heredocs, short Python snippets). This is the minimalism principle applied to the tool surface: one shell primitive covers every file operation, so there are no per-tool quirks for the model to learn (no uniqueness rules, no overwrite refusals, no fuzzy-matching fallbacks), and no tool-specific failure modes to diagnose. The earlier dedicated tools were removed because they were error-prone and the agent reached for Bash anyway. Do not reintroduce dedicated file tools without an explicit design discussion.

---

## `tools/bash.py`

Async shell tool.

### Constants

```python
MAX_OUTPUT_BYTES = 30_000
BASH_TIMEOUT_SECONDS = 3600   # 1-hour hard cap
PIPE_GRACE_SECONDS = 5.0      # grace for pipe-holding background children
TRUNCATION_NOTE = "<NOTE>Output was truncated to {max} bytes. ...</NOTE>"
```

### `async bash(command: str, *, cwd: str | None = None, env: dict[str, str] | None = None) -> str`

Behavior:
- Spawns `asyncio.create_subprocess_shell` with `stdin=DEVNULL, stdout=PIPE, stderr=PIPE, start_new_session=True`.
  - **stdin is `/dev/null`, never inherited** — a command that reads stdin (`cat` with no args, `sudo`, an ssh host-key prompt) would otherwise block forever on input that can never arrive (in TUI mode Textual owns the tty). Such reads now hit EOF immediately and the command fails fast instead of hanging.
  - **`start_new_session=True`** puts the shell in its own process group, so timeout/cancel can `os.killpg(SIGKILL)` the children too. Killing only the shell (the old behavior) orphaned a running child, which kept the captured pipes open and deadlocked the output readers — the kill path itself hung.
- Captures stdout + stderr with two `_drain` reader tasks (not `communicate()`), so the shell's exit and pipe EOF are tracked separately.
- **Pipe-grace rule**: pipe fds are inherited across fork/exec, so when the shell has exited but a surviving background child still holds stdout/stderr open (`server &` without a redirect, a sloppy daemon), the pipes never reach EOF and collection would hang. After `PIPE_GRACE_SECONDS` (5 s) the whole process group is killed and the call returns the output captured so far plus a `<NOTE>` explaining what happened. A properly redirected background job (`nohup … > /tmp/out.log 2>&1 &`) releases the pipes at spawn, returns immediately, and the child survives — this is the supported way to launch daemons.
- **1-hour hard timeout** (`BASH_TIMEOUT_SECONDS = 3600`) — a wedged process's whole group is killed and the tool raises `BashError` (with any partial output attached), which coreouto converts into an error `ToolResult`; the loop wakes and the model decides how to proceed. (The user can also force-stop the loop from the TUI with a double-ESC, which kills the in-flight process much sooner: `build_runtime` passes the turn's `cancel_event` into `set_cancel_event`, and `bash()` polls it every 0.1 s while waiting for the process.)
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

Raises `BashError` on empty command or spawn failure.

### `class BashError(Exception)`

Raised by `bash` on any failure mode.

### JSON schema (`_bash_schema`)

Computed but **not passed to coreouto** (see "A note on schemas" below). Reproduced for reference. Note: there is **no** `env` property and **no** `"default": 60` key in the actual schema — the default is mentioned only inside the description text:

```json
{
  "type": "object",
  "properties": {
    "command":         {"type": "string", "description": "Shell command to execute."},
    "cwd":             {"type": "string", "description": "Override working directory (default: process cwd)."}
  },
  "required": ["command"]
}
```

The handler `_bash_handler(command, cwd=None)` likewise has no `env` parameter, so even if a model sent `env` it would not be forwarded.

### Why `bash` is the only async tool

`asyncio.create_subprocess_shell` integrates cleanly with the TUI's event loop. The media loaders are pure file I/O — running them with `asyncio.to_thread` from the TUI works fine. Keeping `bash` async avoids spawning an extra thread for every shell command. The Computer tool is sync for the same reason as the media loaders (py-Vwayland exposes a blocking IPC API); coreouto runs sync handlers via `asyncio.to_thread`, so even a 60 s screenshot does not stall the event loop.

---

## `tools/media.py`

Read image / video / audio files from disk so the LLM can perceive them directly. Unlike `Bash`, these do **not** return a string — they hand back a `LoadedMedia` record that `registry.py` wraps into coreouto `ContentBlock`s (`ImageBlock` / `VideoBlock` / `AudioBlock`). coreouto then forwards the raw bytes to the provider as a multimodal tool result, so the model receives the actual pixels / frames / waveform rather than a text description.

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
2. Resolves relative paths against `paths_runtime.INVOCATION_CWD`.
3. Raises `MediaViewError` if the path is missing or is a directory.
4. Sniffs the MIME type from the lowercased suffix; raises `MediaViewError` listing the supported extensions if the suffix is unrecognized.
5. Enforces the kind-specific byte cap (`stat().st_size`); raises `MediaViewError` with a downsample hint on overflow.
6. Rejects empty (0-byte) files.
7. Reads the full file into memory via `read_bytes()` and returns `LoadedMedia`.

### `class MediaViewError(Exception)`

Raised by all three loaders on every failure mode above. Carries a human-readable message; the path and kind are embedded in the message text.

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

## `tools/computer.py`

Computer use: operate GUI applications inside **virtual headless displays** so the agent can click, type, and see the screens. Backed by [py-Vwayland](https://github.com/llaa33219/py-Vwayland) (import name `vwayland`) — a bundled Rust/Smithay compositor using pixman CPU rendering: no GPU, no display server, works over SSH and in containers. The exposed surface is a **single `Computer` tool** with an `action` enum (one schema instead of ~10, which keeps the per-request prompt overhead minimal and matches the computer-use convention models are trained on).

**Default dependency, auto-detected**: py-Vwayland is a hard dependency, but its wheel is pure-Python packaging around a bundled native compositor binary that runs only on **Linux x86_64, glibc ≥ 2.28** — so it *installs* everywhere and *works* on one platform. `computer_supported()` probes the platform once per process (cached) and `register_all()` registers the `Computer` tool only when the probe passes: unsupported platforms never advertise the tool to the model (no schema tokens burned, no runtime failure). The **provider format** decides at call time (a module flag refreshed by every `build_runtime`): on `openai` (Chat Completions), which cannot carry multimodal tool results, `screenshot` returns an explanatory notice scoped to the CURRENT provider — including that a retry after a provider switch will work — for the model to relay to the user (GUI control actions still work; the agent just cannot *see*), instead of raising while formatting the request; anthropic / openai-response / google return the image as always. Mid-session provider switches in the TUI therefore take effect on the very next screenshot. The outo preset's tool list is filtered down to actually-registered tools, so an unregistered `Computer` never reaches the preset. Overrides: `MINIOUTO_COMPUTER=0` disables the tool explicitly; `MINIOUTO_COMPUTER=1` skips the probe (e.g. an aarch64 source build of py-Vwayland). Note the probe is static (platform + libc); if the first real `spawn` still fails (sandbox `noexec`, read-only tmp), the call raises `ComputerUseError` with the compositor's log tail and the reset-and-retry semantics below apply.

**Layer rule**: like `media.py`, this module never imports coreouto. It returns a `str` (ordinary actions) or a `Screenshot` dataclass (screenshot action); `registry.py` wraps the latter into `[TextBlock, ImageBlock]` so the model receives the actual pixels.

### Screens (multi-instance registry)

One compositor == one virtual screen == **one app**. `computer.py` keeps a module-level registry (`_SCREENS: id -> Compositor`) so the model can run several GUI apps at once by managing screens explicitly:

- `spawn` creates a screen (`text` = optional label used as its id, e.g. `"browser"`; `coordinate` = optional `[w, h]`, default 1280x720). `kill` tears one down (its app dies with it). `list` shows every screen with size, app pid, and the `WAYLAND_DISPLAY` / `XDG_RUNTIME_DIR` env vars for attaching extra apps from Bash.
- Every other action takes an optional `screen` id. When omitted: zero screens → a default screen is spawned lazily (the simple single-app workflow needs no screen management); exactly one → that one; several → a `ComputerUseError` listing the ids so the model re-issues with `screen=`.
- Screens persist across calls *and* chat turns — GUI app state must survive between actions and between user messages in the TUI.
- All screens are torn down automatically at process exit via vwayland's own `kill_on_exit` atexit hook.
- A screen whose compositor dies mid-session is pruned from the registry and the next call raises `ComputerUseError("Screen '<id>' died… spawn it again…")`.
- One app per screen (`launch` raises while another app runs on that screen — `close_app` first, or `spawn` another screen).

### Concurrency

Every action runs under one module lock (vwayland opens a fresh IPC socket per call and its upstream `_spawned` bookkeeping is not thread-safe, so without the lock concurrent calls could race — even across different screens), and the tool is registered with `parallelizable=False`. The tool is also **outo-preset-only** (`core/runtime.py:OUTO_ONLY_TOOLS`) — parallel subagents driving the screens would click/type over each other.

### `computer(action, coordinate=None, end_coordinate=None, text=None, scroll_direction=None, scroll_amount=3, duration=1.0, screen=None) -> str | Screenshot`

| `action` | Extra fields | Effect |
|---|---|---|
| `screenshot` | — | Fresh frame → `Screenshot` (PNG) → model sees the image |
| `launch` | `text` = command line | Starts a GUI app, returns pid; app stdout/stderr → `<runtime_dir>/app.log` |
| `close_app` | — | SIGTERM the app (returns a note if it survives the timeout) |
| `mouse_move` | `coordinate` | Move pointer to `[x, y]` |
| `left_click` / `right_click` / `middle_click` / `double_click` | `coordinate` optional | Click at `[x, y]`, or at the current pointer position when omitted |
| `left_click_drag` | `coordinate`, `end_coordinate` | Drag between two points |
| `scroll` | `scroll_direction`, `scroll_amount` (detents, default 3), `coordinate` optional (moves pointer first — scroll goes to whatever is under the pointer) | Wheel scroll; `dy > 0` = up |
| `type` | `text` | Types literally, US layout; shift-symbols handled, `"\n"` = Enter |
| `key` | `text` | xdotool-style key spec: `"enter"`, `"f5"`, `"ctrl+s"`, `"alt+f4"`. Aliases mapped: `control→ctrl`, `cmd/command→super`, `option→alt`, `pgup/pgdn`, `del/ins` |
| `wait` | `duration` (0 < s ≤ 30) | Sleep so the app can react |
| `resize` | `coordinate` = `[w, h]` | Resize the target screen (16..16384); the app is reconfigured |
| `screen_info` | — | Target screen's size, display name, app pid, env vars for Bash-attached apps |
| `spawn` | `text` = optional label (becomes the id), `coordinate` = optional `[w, h]` | Create another screen (for a second app) |
| `kill` | `screen` = id | Tear down a screen and its app |
| `list` | — | Every screen: id, size, app pid, attach env vars |

All actions except `spawn`/`list` accept `screen` (target screen id); the omission rules are in "Screens" above. Coordinates are **logical screen pixels**, `(0, 0)` top-left, clamped to the screen bounds. All argument errors (missing `coordinate`, unknown key name, unknown screen id, bad size) raise `ComputerUseError` with an actionable message; vwayland's own exceptions are wrapped into `ComputerUseError` as well.

### Provider support

`screenshot` returns an `ImageBlock`, so computer use needs a **vision-capable** provider — the same matrix as the Image tool above (Anthropic/Google yes; OpenAI Responses yes; OpenAI Chat Completions rejects all multimodal blocks). On a non-vision provider the screenshot call succeeds but the *next* LLM call raises `ValueError`.

---

## `tools/registry.py`

Wires the bash/media/computer tools into coreouto's tool registry.

### `register_all()`

Idempotent: calls `_register_if_missing(name, handler, schema, description)` for `Bash`, `Image`, `Video`, `Audio`, and — when `computer_supported()`; screenshot availability is decided at call time from the outo provider's `api_format` — `Computer` (with `parallelizable=False`). (The `call_subagent` tool is registered separately in `core.runtime.build_runtime` because it needs the subagent config to be built first.)

### `_register_if_missing(name, handler, schema, description, *, parallelizable=True)`

Skips if `co.get_tool(name)` is already set; otherwise calls `co.register_tool(name, description=description, parallelizable=parallelizable)(handler)` — **the `schema` parameter is accepted but silently discarded**. This is what makes repeated `build_runtime` calls safe in TUI mode.

#### A note on schemas (dead code)

The `_xxx_schema()` functions are invoked at registration time (`_register_if_missing("Bash", _bash_handler, _bash_schema(), _bash_description())`), but their return values are never forwarded to `coreouto.register_tool`. Only the handler's Python type hints and the `description` string reach the model. The schema dicts in this file are effectively documentation-only. Do not rely on them affecting model behavior; if you need the model to see a parameter restriction, encode it in the description text.

### Handlers (private)

| Tool | Handler | Signature |
|---|---|---|
| `Bash` | `async _bash_handler(command, cwd=None) -> str` | async (no `env` — the handler signature does not expose it even though `bash()` does) |
| `Image` | `_image_handler(file_path) -> list[co.ContentBlock]` | sync, **multimodal** — returns `[TextBlock, ImageBlock]` |
| `Video` | `_video_handler(file_path) -> list[co.ContentBlock]` | sync, **multimodal** — returns `[TextBlock, VideoBlock]` |
| `Audio` | `_audio_handler(file_path) -> list[co.ContentBlock]` | sync, **multimodal** — returns `[TextBlock, AudioBlock]` |
| `Computer` | `_computer_handler(action, coordinate=None, end_coordinate=None, text=None, scroll_direction=None, scroll_amount=3, duration=1.0) -> str \| list` | sync; returns `str` for ordinary actions, **multimodal** `[TextBlock, ImageBlock]` for `screenshot` |

The media handlers are the **only** handlers in this file that return something other than `str`. They delegate the file read to `tools.media.load_*` (which returns a `LoadedMedia`), then build a two-element block list: a `TextBlock` caption (path + byte count + MIME) and the binary block. coreouto forwards the list to the provider as a multimodal tool result. Do **not** refactor these to return `str` — that would discard the media payload and silently degrade the tools to "the file exists" no-ops. Contract: [coreouto `tools.md` — Multimodal tool results](https://github.com/llaa33219/coreouto/blob/main/docs/tools.md#multimodal-tool-results).

### Descriptions (what the LLM actually sees)

Each description includes the tool's restrictions inline. Verbatim from `registry.py`:

| Tool | Description (verbatim) |
|---|---|
| `Bash` | "Run a shell command. Captures stdout and stderr; exits with the command's exit code. stdin is /dev/null, so commands that prompt for input fail fast — pass input via flags or files instead. Hard 1-hour timeout — a command that exceeds it is killed (with its whole process group) and returns an error. To run something in the background, ALWAYS redirect its output (nohup … > /tmp/out.log 2>&1 &) and poll the log file; a background process that keeps stdout/stderr open is killed shortly after the shell exits. Output >30KB is truncated with a note. Default cwd is the directory miniouto was invoked from. This is the ONLY file-manipulation tool: read with `cat`/`grep`/`find`, create with `cat > file <<'EOF'` or `tee`, edit with `sed -i` or a short Python snippet, delete with `rm`. Also use it for `git`, `pytest`, package managers, etc." |
| `Image` | "View an image file and return it to the model so it can actually be seen. Supports PNG, JPEG, GIF, WebP. Capped at 20 MB. Pass an absolute path, or a path relative to the directory miniouto was invoked from. The file's raw bytes are uploaded to the provider as an image content block — the model receives the pixels, not a text description. For unsupported formats or oversized files, convert first with Bash (e.g. ImageMagick `convert`, Pillow)." |
| `Video` | "View a video file and return it to the model so it can actually be perceived. Supports MP4, MOV, WebM. Capped at 50 MB. Pass an absolute path, or a path relative to the directory miniouto was invoked from. The file's raw bytes are uploaded to the provider as a video content block. For unsupported formats or oversized files, downsample first with Bash (e.g. ffmpeg)." |
| `Audio` | "View an audio file and return it to the model so it can actually be heard. Supports WAV, MP3. Capped at 25 MB. Pass an absolute path, or a path relative to the directory miniouto was invoked from. The file's raw bytes are uploaded to the provider as an audio content block. For unsupported formats or oversized files, downsample first with Bash (e.g. sox, ffmpeg)." |
| `Computer` | "Operate a GUI application inside a virtual 1280x720 display (headless; one screen shared across calls, spawned lazily on first use). Core loop: `launch` an app, then repeat screenshot -> act -> screenshot. ALWAYS take a screenshot before clicking anything — click coordinates come from the latest screenshot; (0, 0) is the top-left corner, units are screen pixels, positions are clamped to the screen. After any action that changes the screen, screenshot again rather than assuming the result. …" (followed by the per-action field list, the vision-provider requirement, and the `computer`-extra install hint — see `_computer_description()` in `registry.py` for the full verbatim text) |

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
7. Add a `Bash`-style test for the new tool's edge cases (none exist yet, so this is a chance to start the test suite).
8. If the new tool returns multimodal content, note that provider support varies (see the matrix in `tools/media.py` below). Do **not** put provider names in the tool description or style prompts — the agent cannot introspect its own provider, so such hints are unactionable. Let provider rejections surface naturally as `ValueError` at call time; that error is the teaching signal. Document the matrix here in `docs/tools.md` for human operators instead.
