# Tools

The `tools/` subpackage implements the tools the agent can invoke: one shell tool (`Bash`), three media viewers (`Image` / `Video` / `Audio`), the GUI tool (`Computer`), and one session tool (`continue_session`). Each tool is a plain Python function (or async function for `Bash`) registered with coreouto via `tools/registry.py`.

```
src/miniouto/tools/
├── __init__.py
├── ascii_art.py      # .art codec: PNG decode + Unicode sub-cell render (pure stdlib)
├── bash.py           # async bash(command, *, cwd, env)
├── computer.py       # computer(action, …) — virtual-display GUI control
├── continue_session.py  # continue_session(summary_dir) — the only way a session changes
├── media.py          # load_image/load_video/load_audio — read media bytes (pure stdlib)
└── registry.py       # register_all() — wires tools into coreouto
```

**Layer rules:**
- `bash.py`, `media.py`, `computer.py`, `continue_session.py` and `ascii_art.py` are **pure stdlib** (no coreouto dependency). They are the only tools layer code that touches the filesystem outside `storage/`. `ascii_art.py` also imports nothing from `miniouto` — it is a pure codec over bytes and strings, so `computer.py` (its caller) stays below it in the dependency graph.
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
- Captures stdout + stderr with two `_drain` reader coroutines (not `communicate()`), so the shell's exit and pipe EOF are tracked separately.
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

`Computer(action="screen")` is the one multimodal-looking channel that **escapes this matrix**: it returns a plain `str` (path + a short preview), so it works on every provider, including OpenAI Chat Completions, where `Computer(action="screenshot")` degrades to a notice.

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

Every action runs under one module lock (vwayland opens a fresh IPC socket per call and its upstream `_spawned` bookkeeping is not thread-safe, so without the lock concurrent calls could race — even across different screens), and the tool is registered with `parallelizable=False`. The tool is **platform-gated** (`core/runtime.py:PLATFORM_GATED_TOOLS` — appended only when the platform actually registered it) and available to every run, outo and delegated role alike.

### `computer(action, coordinate=None, end_coordinate=None, text=None, scroll_direction=None, scroll_amount=3, duration=1.0, screen=None, chars=100, color=True, glyph="mix") -> str | Screenshot`

| `action` | Extra fields | Effect |
|---|---|---|
| `screenshot` | — | Fresh frame → `Screenshot` (PNG) → model sees the image; the same frame also gets a best-effort `.art` sibling (`Screenshot.art_path`), and the result text ends with `Saved to: <path>` |
| `screen` | `chars`, `color`, `glyph` | Render the **current** screen as Unicode sub-cell art, write it to `~/.miniouto/computer-screens/<screen>-<ts>.art`, and return a short `str` (see below) |
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

### The `screen` action — text capture for providers that cannot see

`screenshot` hands the model **pixels**, which not every provider format can carry. `screen` hands it **text** instead: the same blocking `comp.screenshot()` frame is decoded and re-rendered as sub-cell art, written to disk, and returned as an ordinary `str`. That makes it usable exactly where the image path is not — **it deliberately bypasses the openai-format image gate**, because a `str` result carries no multimodal block for the provider to reject.

| Parameter | Meaning |
|---|---|
| `chars` | Art width target in terminal cells, clamped to `ART_MIN_CHARS = 16` … `ART_MAX_CHARS = 200`, default `ART_DEFAULT_CHARS = 100`. Below 16 a 1280px source loses every glyph of GUI text; above 200 the art stops fitting any terminal (and the `.art` file stops being human-readable) |
| `color` | `True` → `subcell-truecolor` (run-length SGR + `█`/octants/braille); `False` → `ramp-plain` (no escapes at all, one char per cell from `" .:-=+*#%@"`) |
| `glyph` | `"mix"` (default) / `"octant"` / `"braille"` — which sub-cell glyph family the truecolour renderer picks per cell. Only `screen` and the `screenshot` sibling art read it; every other action ignores `chars`, `color` and `glyph` alike |

#### The precision model: 8 sub-pixels per cell

`render_halfblock` puts **two** vertical source pixels in one cell (`▀` + fg/bg). The default renderer, `render_subcell`, puts **eight** — a 2x4 grid — because a truecolour terminal cell can address all eight sub-cells at once. Each sub-cell is box-averaged over its own source rect, never point sampled. Consequences worth knowing:

- **Same rows as half-block.** `rows = max(1, round(cols * height / width / 2.0))` (`ascii_art.subcell_rows`, the `render_halfblock` formula verbatim), so a 1280x720 capture at 100 cols is `grid: 100x28` in **both** modes. The `/ 2.0` is only the terminal cell's 1:2 aspect correction — a cell is 2x4 source pixels, but those sub-pixels are square, so one cell covers the same source rect either way. Sub-cell art buys resolution *inside* the cell, never fewer rows; halving them would squash every screen to half height. The `.art` meta's `cell` field is the sub-pixel's source size, `round(width / (2*cols))` x `round(height / (4*rows))` — `6x6` for the 1280x720 @ 100 cols example.
- **Two colours per cell, not one.** Every cell is reduced to an (fg, bg) pair plus an 8-bit sub-cell mask by a 2-means pass seeded on the farthest pair of the eight samples (3 Lloyd iterations, brighter entry as fg). The mask then picks the glyph, so **colour is preserved rather than merely encoded**:

| Cell looks like | Glyph | Chosen when |
|---|---|---|
| one flat colour | `█` (U+2588) | every sub-cell within `UNIFORM_SPREAD = 12.0` of the cell mean |
| two crisp solid regions | **BLOCK OCTANT** (U+1CD00…U+1CDE5, Unicode 16.0) | entry separation ≥ `OCTANT_MIN_SEPARATION = 60.0` **and** worst intra-cluster error ≤ `OCTANT_MAX_INTRA_ERROR = 24.0` |
| a blend, a gradient, antialiased text | **BRAILLE** (`U+2800 + mask`) | otherwise — 8 halftone dots that optically blend fg and bg |

  BLOCK OCTANT digits are **row-major** (`1,2` = top row left to right, `7,8` = bottom row); braille dots and the renderer's internal mask are **column-major** (`bit i` = sub-cell `i`, left column rows 0-3 then right column rows 0-3). `ascii_art._name_mask_to_internal` is the one permutation between the two, and both glyph families read the same internal mask, so they are interchangeable. Unicode encodes only 230 of the 256 masks as BLOCK OCTANT; the **26 unencoded masks fall back to a braille pattern**. The full rule, thresholds and format spec are in `docs/storage.md` § "The sub-cell glyph set".
- **Every patterned cell is readable.** The glyph class is picked first, then the fg/bg pair is contrast-boosted (`ascii_art._boost_pair`) — 2-means minimises error, so a subtle cell would otherwise get two nearly identical entries and an invisible mask. The pair is widened **symmetrically about the cell mean** until it spans `MIN_LUMA_DELTA = 40.0` of Rec. 601 luminance (primary) and `MIN_PAIR_DISTANCE = 50.0` of colour: along its own direction so hue is preserved (capped at `MAX_CONTRAST_BOOST = 2.0`, so a noisy cell cannot explode), then along the neutral axis for the luminance shortfall, both clamped to the `[0, 255]` room around the mean. A pair that cannot reach the floor in gamut — pinned at black/white, or structureless noise — collapses the cell to the mean `█`, because a smooth block beats visible noise. Uniform and degenerate (empty/full mask) cells are never boosted.
- **The font caveat is real.** BLOCK OCTANT only exists from Unicode 16.0 (2024). On an older font or terminal every chosen octant renders as tofu. `glyph="braille"` is the escape hatch: it forces dots everywhere, which is never worse than the old half-block. `glyph="octant"` does the opposite (blocks wherever a mask applies), and `glyph="mix"` is the default rule above.
- **`halfblock-truecolor` is still there.** `render_halfblock` and `MODE_COLOR = "halfblock-truecolor"` are unchanged, so existing `.art` files parse and render byte-identically, and `miniouto computer show` needs no mode switch.

The result string is a header (`Screen art captured (1280x720 -> 100x28 chars, subcell truecolor).` + `Saved to: <path>` + the "coordinates still come from screenshot" note), then **at most `_ART_PREVIEW_ROWS = 24` rows** of preview — sized so the whole result stays under the `_ART_RESULT_LIMIT = 3800` char tool-result cap, with a `… N more rows are in the file above.` tail. **The full art lives in the file**, not in the transcript: render it with `miniouto computer show [PATH] [--plain] [--cols N] [--head N]` (newest capture by default), which is also how a delegated `miniouto chat --role <tag>` run reads what the orchestrator captured, since a `.art` file is plain text. Art carries **no clickable coordinates** — those still come from `screenshot`.

**`screenshot` writes the same art as a best-effort sibling — and the raw PNG next to it.** `_write_art_best_effort` renders the capture's `.art`; `_write_png_best_effort` writes the same frame's bytes to a sibling `.png` and hands the path back on `Screenshot.png_path`. `registry.py` appends both to the caption text: `Image saved to: <path>.png` first, then the `Saved to: <path>.art` line LAST (the older, documented hook). Both are best effort on purpose: the screenshot's pixels are the deliverable, so an undecodable PNG, a full disk or a read-only home costs the sibling files and nothing else — never the capture. The directory is pruned to the newest `MAX_CAPTURE_FILES = 100` files after every write (oldest-first, pairs retire together), so a screenshot-heavy agent cannot grow it without bound.

**The TUI renders the PNG as real terminal graphics where the terminal allows it.** An expanded screenshot row mounts an image widget on the raw PNG — pixel-accurate, not a unicode approximation — filling the log's content width (`width: 100%`, `height: auto`, so the capture's aspect decides the height). The row's caption text keeps the path. ANY failure in that lane — no `textual-image`, a non-graphics terminal, a missing PNG sibling, a PIL decode error, a mount failure — falls through to the `.art` text render unchanged, so a tmux or non-Kitty session looks exactly as before. The `screen` action writes only `.art`, so it always renders as text art even on a graphics terminal: the model's text view stays stable.

**The image is a SIBLING row in the log, not a child of the text row.** A child widget is laid out at the TOP of its parent's content area — exactly where the parent's own caption is, so the caption and the image were drawn ON TOP OF EACH OTHER. And a `Static` with any child becomes a container, and a container whose only child is `display: none` computes its auto height as ZERO, so hiding the image collapsed the whole row out of the log. As a sibling row the image has its own lines, and hiding it leaves the text row's height alone.

**The TGP image is uploaded ONCE.** `textual_image`'s stock TGP widget re-sends the whole picture on every repaint (it deletes the image and rebuilds the renderable, which base64-encodes the PNG again) — ~1.8 MB per repaint on a 1280x720 capture, so scrolling the log re-uploaded it over and over. `cli/graphics.py:CachedTGPImage` keeps the renderable for the widget's lifetime, so the image id survives and only the cheap placement is re-emitted; the image is deleted from the terminal when the row goes away. Measured: 47.8 ms per repaint with the stock widget, 39.4 ms with this one, against 37.6 ms for a 33-line text widget of the same size — the picture adds ~1.8 ms to a repaint instead of ~10 ms of CPU plus a 1.8 MB upload to the terminal. The Sixel path caches its encoded sixels already and needed no change.

**The mode is detected once, before Textual starts.** `tui.detect_graphics_mode()` (called from `run_tui()`) asks the terminal once whether it speaks TGP or Sixel and caches the answer as `GRAPHICS_MODE_TGP` / `GRAPHICS_MODE_SIXEL` / `GRAPHICS_MODE_TEXT`. The render path only reads that value. The ordering is load-bearing: the probe reads the terminal's reply on **stdin**, and Textual's reader thread takes stdin over the moment the app runs, so a render-time probe never sees the reply and only burns its 2-second timeout — exactly the "screenshot takes a while to appear" symptom. Detection also pre-imports `textual_image.widget` (Pillow) so the first screenshot does not stall on an import. Any detection failure is `GRAPHICS_MODE_TEXT`: the `.art` lane, never an error.

**Sixel wins when the terminal offers both.** TGP renders better, but its image-placement scheme needs a terminal that implements the placeholder protocol properly — Konsole and wezterm *report* TGP support while not implementing it, and the image never appears there. Sixel is a byte stream any Sixel terminal renders, so it is the safe choice whenever available; TGP is for the terminals without Sixel (in practice Kitty). The mode also picks the widget class (`TGPImage` vs `SixelImage`), so the renderer and the probe cannot disagree.

The image gate is checked **before** the `.art` gate: a capture whose `.art` write failed still has its pixels, and those are what the graphics lane shows.

Because the `Saved to: <path>.art` line is the exact hook the TUI's `art_path_in_result` matches, an expanded tool row renders the truecolour art for **any** Computer result that names a `.art` file, not only `screen`. The TUI hands the body to Textual's `Text.from_ansi`, so the `█` / octant / braille glyphs and their SGR colours need no TUI-specific handling — the same file renders identically in `miniouto computer show`.

Decoding is pure stdlib in `tools/ascii_art.py`: `decode_png_rgba` is miniouto's own 8-bit non-interlaced PNG decoder (colour types 0/2/3/4/6, all five scanline filters), because py-Vwayland exposes a screenshot **only** as `Image.png_bytes` and the decoder must not need Pillow — Pillow IS now a transitive dependency via `textual-image` (the TUI's graphics lane), but the `.art` codec predates it and stays pure stdlib so the tool layer never depends on an imaging library. Every input it cannot faithfully reproduce raises a `ValueError` naming the offending property — a screenshot that decodes to garbage is worse than one that refuses to decode — and the tool surfaces that as `ComputerUseError`. The decode runs on a worker thread in the caller (`asyncio.to_thread`); a straightforward pure-Python defilter is the right trade against correctness and an actionable error. The `.art` file format is documented in `docs/storage.md`.

### Provider support

`screenshot` returns an `ImageBlock`, so seeing the screen needs a **vision-capable** provider — the same matrix as the Image tool above (Anthropic/Google yes; OpenAI Responses yes; OpenAI Chat Completions rejects all multimodal blocks, where `screenshot` instead returns an explanatory notice and `screen` remains the working channel). On a non-vision provider the screenshot call succeeds but the *next* LLM call raises `ValueError`.

---

## `tools/registry.py`

Wires the bash/media/computer tools into coreouto's tool registry.

### `register_all(api_format=None)`

Idempotent: calls `_register_if_missing(name, handler, schema, description)` for `Bash`, `Image`, `Video`, `Audio`, `continue_session`, and — when `computer_supported()` — `Computer` (with `parallelizable=False`). `api_format` refreshes a module flag the one registered Computer handler reads **at call time** to decide whether `screenshot` may return an image or must return a notice; a mid-session provider switch therefore takes effect on the very next call. Delegation is **not** a tool at all: it rides on `Bash` as `miniouto chat "<brief>" --style <style> --role <tag>`.

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
| `Computer` | `_computer_handler(action, coordinate=None, end_coordinate=None, text=None, scroll_direction=None, scroll_amount=3, duration=1.0, screen=None, chars=100, color=True) -> str \| list` | sync; returns `str` for ordinary actions **and** for `screen` (a text result), **multimodal** `[TextBlock, ImageBlock]` only for `screenshot` |
| `continue_session` | `continue_session(summary_dir)` (imported straight from `tools/continue_session.py` — no wrapper handler) | sync, plain **text** result, **no provider gate** — it never returns a multimodal block, so it works identically on every `api_format` |

The media handlers (and `Computer`'s `screenshot` branch) are the **only** handlers in this file that return something other than `str`. They delegate the file read to `tools.media.load_*` (which returns a `LoadedMedia`), then build a two-element block list: a `TextBlock` caption (path + byte count + MIME) and the binary block. coreouto forwards the list to the provider as a multimodal tool result. Do **not** refactor these to return `str` — that would discard the media payload and silently degrade the tools to "the file exists" no-ops. Contract: [coreouto `tools.md` — Multimodal tool results](https://github.com/llaa33219/coreouto/blob/main/docs/tools.md#multimodal-tool-results).

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

## `tools/continue_session.py`

```python
continue_session(summary_dir: str) -> str
```

Sync, registered unconditionally, in `BASE_TOOLS` — so outo and every delegated `--role` run have it. It is the **only** way a session changes.

**What it does, in order:**

1. **Validate.** `_resolve` expands `~`, resolves a relative path against `INVOCATION_CWD`, and requires an existing directory holding at least one `*.md`. Anything else (empty, missing, not a directory, no markdown) returns the reason as the tool result — never an exception — so the model can write the documents and retry. It deliberately does *not* judge whether the summary is *good*: only the session that reads it can.
2. **Mint** `cont-<YYYYMMDD-HHMMSS>-<6hex>`.
3. **Insert the continuation message** into the new session's inbox — `kind=inbox.ALARM_KIND`, `source="summary"`, `meta={"from": <old session>}` — built by `core/summary.py:build_continuation_message` (header `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` + `New session opened from a summary — continue the previous work here.`).
4. **Record the hand-off** via `summary.request_handoff(new_session, from_session=..., summary_dir=str(directory))`. The loop layer never consumes it; the entry layer (`cli/chat.py:_run_handoffs`) performs the session transition.
5. **Ack.** The returned text names the old and new sessions and the folder, repeats that a delegation contract (role / style / alarm session / result file) travelled along, and ends with "do no more work here, end your turn".

**Why the loop ends there.** `core/chat.py` builds the turn's cancel event as a `_StopOnHandoff` — a `threading.Event` whose `is_set()` ORs the caller's cancel event with its own hand-off flag — and the `AFTER_TOOL_CALL` dispatcher fires that flag when it sees a pending hand-off. The loop stops cooperatively at its next step rather than letting the model burn the rest of a nearly-full context on work the new session is about to redo. The resulting `LoopCancelledError` is re-classified at the call site into a clean `status="done"` turn; the hand-off stays pending for the entry layer.

**Invocation points.** Exactly two, at the end of a turn: `cli/chat.py:_run_handoffs` (`_apply_handoff` moves `opts.session`, re-registers the run contract under the new session, updates `settings.session`, and prints `------summary → <new>------` + `summary: <dir>`) and `cli/tui.py:ChatTUI._apply_handoff` (mounts the same marker in the old conversation, persists it for replay, re-pins the new session, and lets the 1 s inbox poll auto-deliver the continuation).

## Delegation is a Bash command, not a tool

There is **no** `call_subagent` — it was deleted along with the in-process job registry. Delegation happens because the model runs the harness through the shell:

```
Bash: miniouto chat "<brief>" --style <style> --role <tag> [--bg] --alarm-session <session>
```

That is the same `chat` command the user runs — same `ChatOptions` assembly, same `run_turn_chain`, same watchdog — in **its own OS process**. Consequences worth knowing when you write or debug a tool:

- **One brief per invocation; no fan-out parameter.** Parallelism = several `miniouto chat --role` commands in one Bash call, or several Bash calls. Do not reintroduce a multi-brief flag.
- The run **starts with no history**, so the brief is the whole input. An under-specified brief produces garbage; that is a prompting problem, not a runtime one.
- `--role <tag>` selects a top-level `<tag>` body from the active style as the system prompt, and an unknown tag **fails before the turn** (`cli/chat.py:_validate_role`), so a typo costs no model call.
- `--bg` is **detach-safe by construction**: the child gets `start_new_session=True` and file stdio (`spawn-logs/<id>.log`), because `tools/bash.py` kills whole process groups and grace-kills pipe-holding children — so a background run survives only with both halves.
- Two output channels, always both: an answer file under `~/.miniouto/subagent-results/` and **exactly one** inbox message whose first line is `[SYSTEM ALARM - THIS IS NOT A USER MESSAGE]` and which names that file. A third channel is only a promise: the `spawn-logs/<id>.json` liveness marker.
- Because the run is a separate process, `register_all`/preset setup is independent of it — it builds its **own** runtime, and it gets the **identical tool list** to outo (`Computer` included; separate processes own separate vwayland screens). Full reference in `docs/cli.md`.

## Path resolution

Every tool resolves relative paths against `paths_runtime.INVOCATION_CWD` (the cwd captured at miniouto import time). This is the cwd the user invoked miniouto from — not the cwd at tool-call time, which can drift if the agent's earlier `Bash` calls did `cd`.

If you need the agent to operate relative to a different directory, pass an absolute path or have a `Bash` call do the `cd` first.

---

## Adding a new tool

1. Create `src/miniouto/tools/<name>.py` with a single function `<name>(**kwargs) -> str` (or `async def`). Keep it **pure stdlib** — no `coreouto` import. If the tool needs to return media (image/video/audio bytes), return a plain data structure (like `media.py`'s `LoadedMedia`) and let `registry.py` build the `co.ContentBlock`s. See `tools/media.py` for the pattern.
2. Add the function's description, schema, and handler to `tools/registry.py`. Add `_<name>_handler`, `_<name>_schema`, `_<name>_description` and wire them via `_register_if_missing` inside `register_all`. (Note: per "A note on schemas" above, the schema dict is currently discarded at registration — the description string is what reaches the model.) **For multimodal tools**, the handler returns `list[co.ContentBlock]` instead of `str` — see the `Image` / `Video` / `Audio` handlers for the exact shape.
3. Add the name to `core/runtime.BASE_TOOLS` — every run gets it (an interactive outo turn **and** a delegated `miniouto chat --role <tag>` run, since there is one preset and two modes that differ only in the system prompt).
4. If the tool's registration is **platform-conditional** (like `Computer`), add its name to `core/runtime.PLATFORM_GATED_TOOLS` instead; `build_runtime` appends that list in every mode, filtered down to what actually registered, so an unsupported platform never sees it. **Do not confuse this with `parse_style`**, which only resolves the style *prompts* (the roles `chat --role` can select), not the tool lists.
5. Add the tool name to `_LOGGABLE_TOOL_NAMES` and the tool-name set in `_make_tool_call_dispatcher` (plus a branch in `_short_arg_summary`) in `core/chat.py`, so loop events and failure diagnostics render the new tool nicely. (The media tools `Image` / `Video` / `Audio` are examples of this wiring.)
6. Update `default_style/*.md` if the tool's name or behavior should be documented to the model.
7. Add a `Bash`-style test for the new tool's edge cases (none exist yet, so this is a chance to start the test suite).
8. If the new tool returns multimodal content, note that provider support varies (see the matrix in `tools/media.py` below). Do **not** put provider names in the tool description or style prompts — the agent cannot introspect its own provider, so such hints are unactionable. Let provider rejections surface naturally as `ValueError` at call time; that error is the teaching signal. Document the matrix here in `docs/tools.md` for human operators instead.
