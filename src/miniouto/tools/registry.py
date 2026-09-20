"""Register the bash/media/computer tools as coreouto tools."""

from __future__ import annotations

import coreouto as co

from .bash import bash
from .computer import (
    ComputerAction,
    Screenshot,
    ScrollDirection,
    computer,
    computer_supported,
)
from .media import load_audio, load_image, load_video


def register_all(api_format: str | None = None) -> None:
    """Register Bash, Image, Video, Audio, Computer as coreouto tools.

    Idempotent: if a name is already registered, leave it alone. Computer
    is registered only when BOTH gates pass:

    - `computer_supported()` — on platforms where the bundled compositor
      cannot run, the tool is never advertised to the model.
    - `api_format != "openai"` — openai Chat Completions rejects multimodal
      tool results (the provider raises "does not support multimodal tool
      results (image block detected)" while formatting the request), so a
      `screenshot` would kill the whole turn. anthropic / openai-response /
      google all accept image tool results. Not advertising the tool on
      such providers trades a mid-turn crash for coreouto's unknown-tool
      teaching error — the same philosophy as the platform gate.
    """

    _register_if_missing("Bash", _bash_handler, _bash_schema(), _bash_description())
    _register_if_missing("Image", _image_handler, _image_schema(), _image_description())
    _register_if_missing("Video", _video_handler, _video_schema(), _video_description())
    _register_if_missing("Audio", _audio_handler, _audio_schema(), _audio_description())
    if computer_supported() and api_format != "openai":
        # parallelizable=False: the virtual screen is one shared, order-
        # sensitive resource — it must never run concurrently with another
        # tool call.
        _register_if_missing(
            "Computer",
            _computer_handler,
            _computer_schema(),
            _computer_description(),
            parallelizable=False,
        )


def _register_if_missing(
    name: str, handler, schema: dict, description: str, *, parallelizable: bool = True
) -> None:
    if co.get_tool(name) is not None:
        return
    co.register_tool(name, description=description, parallelizable=parallelizable)(handler)


async def _bash_handler(command: str, cwd: str | None = None) -> str:
    return await bash(command, cwd=cwd)


# The Image/Video/Audio handlers below return list[co.ContentBlock] (a TextBlock
# caption + the binary block), NOT a plain str. coreouto forwards multimodal
# tool results to the provider so the model actually perceives the media.
# Do NOT "simplify" them to return str — that would discard the payload.
# Contract: coreouto/docs/tools.md, "Multimodal tool results".


def _image_handler(file_path: str) -> list:
    media = load_image(file_path)
    return [
        co.TextBlock(
            text=(
                f"Image at {media.path} ({len(media.data)} bytes, "
                f"{media.mime_type})."
            )
        ),
        co.ImageBlock(data=media.data, mime_type=media.mime_type),
    ]


def _video_handler(file_path: str) -> list:
    media = load_video(file_path)
    return [
        co.TextBlock(
            text=(
                f"Video at {media.path} ({len(media.data)} bytes, "
                f"{media.mime_type})."
            )
        ),
        co.VideoBlock(data=media.data, mime_type=media.mime_type),
    ]


def _audio_handler(file_path: str) -> list:
    media = load_audio(file_path)
    return [
        co.TextBlock(
            text=(
                f"Audio at {media.path} ({len(media.data)} bytes, "
                f"{media.mime_type})."
            )
        ),
        co.AudioBlock(data=media.data, mime_type=media.mime_type),
    ]


def _bash_description() -> str:
    return (
        "Run a shell command. Captures stdout and stderr; exits with the "
            "command's exit code. stdin is /dev/null, so commands that "
            "prompt for input fail fast — pass input via flags or files "
            "instead. Hard 1-hour timeout — a command that exceeds it is "
            "killed (with its whole process group) and returns an error. "
            "To run something in the background, ALWAYS redirect its "
            "output (nohup … > /tmp/out.log 2>&1 &) and poll the log file; "
            "a background process that keeps stdout/stderr open is killed "
            "shortly after the shell exits. "
        "Output >30KB is truncated with a note. Default cwd is the "
        "directory miniouto was invoked from. This is the ONLY "
        "file-manipulation tool: read "
        "with `cat`/`grep`/`find`, create with `cat > file <<'EOF'` or "
        "`tee`, edit with `sed -i` or a short Python snippet, delete with "
        "`rm`. Also use it for `git`, `pytest`, package managers, etc."
    )


def _bash_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute."},
            "cwd": {
                "type": "string",
                "description": "Override working directory (default: process cwd).",
            },
        },
        "required": ["command"],
    }


def _image_description() -> str:
    return (
        "View an image file and return it to the model so it can actually be "
        "seen. Supports PNG, JPEG, GIF, WebP. Capped at 20 MB. Pass an "
        "absolute path, or a path relative to the directory miniouto was "
        "invoked from. The file's raw bytes are uploaded to the provider as "
        "an image content block — the model receives the pixels, not a text "
        "description. For unsupported formats or oversized files, convert "
        "first with Bash (e.g. ImageMagick `convert`, Pillow)."
    )


def _image_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path of the image file to view.",
            },
        },
        "required": ["file_path"],
    }


def _video_description() -> str:
    return (
        "View a video file and return it to the model so it can actually be "
        "perceived. Supports MP4, MOV, WebM. Capped at 50 MB. Pass an "
        "absolute path, or a path relative to the directory miniouto was "
        "invoked from. The file's raw bytes are uploaded to the provider as "
        "a video content block. For unsupported formats or oversized files, "
        "downsample first with Bash (e.g. ffmpeg)."
    )


def _video_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path of the video file to view.",
            },
        },
        "required": ["file_path"],
    }


def _audio_description() -> str:
    return (
        "View an audio file and return it to the model so it can actually be "
        "heard. Supports WAV, MP3. Capped at 25 MB. Pass an absolute path, "
        "or a path relative to the directory miniouto was invoked from. The "
        "file's raw bytes are uploaded to the provider as an audio content "
        "block. For unsupported formats or oversized files, downsample "
        "first with Bash (e.g. sox, ffmpeg)."
    )


def _audio_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path of the audio file to view.",
            },
        },
        "required": ["file_path"],
    }


def _computer_handler(
    action: ComputerAction,
    coordinate: list[int] | None = None,
    end_coordinate: list[int] | None = None,
    text: str | None = None,
    scroll_direction: ScrollDirection | None = None,
    scroll_amount: int = 3,
    duration: float = 1.0,
    screen: str | None = None,
) -> str | list:
    result = computer(
        action,
        coordinate=coordinate,
        end_coordinate=end_coordinate,
        text=text,
        scroll_direction=scroll_direction,
        scroll_amount=scroll_amount,
        duration=duration,
        screen=screen,
    )
    if isinstance(result, Screenshot):
        # Same multimodal contract as the media handlers above: the model
        # must receive the pixels, not a description of them.
        return [
            co.TextBlock(
                text=(
                    f"Screenshot of the virtual display "
                    f"({result.width}x{result.height} pixels). "
                    "Click coordinates for the next action refer to this "
                    "image; (0, 0) is the top-left corner."
                )
            ),
            co.ImageBlock(data=result.data, mime_type=result.mime_type),
        ]
    return result


def _computer_description() -> str:
    return (
        "Operate GUI applications inside virtual headless displays "
        "(screens). One screen runs ONE app; screens are spawned lazily "
        "(default 1280x720) and shared across calls. "
        "Core loop: `launch` an app, then repeat screenshot -> act -> "
        "screenshot. ALWAYS take a screenshot before clicking anything — "
        "click coordinates come from the latest screenshot; (0, 0) is the "
        "top-left corner, units are screen pixels, positions are clamped "
        "to the screen. After any action that changes the screen, "
        "screenshot again rather than assuming the result. "
        "Multiple apps: spawn an extra screen per app (action=spawn, "
        "text=optional label used as its id, coordinate=optional "
        "[width, height]) and pass screen=\"<id>\" on every action "
        "targeting it. With exactly one screen up you may omit screen; "
        "with several, omitting it is an error. "
        "Actions and their fields: "
        "screenshot: capture the screen; you receive the image itself. "
        "launch: start a GUI app (text = command line, e.g. \"firefox\" "
        "or \"code --new-window\"). One app per screen — close_app "
        "before launching another on the same screen, or spawn a new "
        "screen. Apps take a moment to appear: wait, then screenshot. "
        "close_app: terminate the launched app. "
        "mouse_move: move the pointer to coordinate [x, y]. "
        "left_click / right_click / middle_click / double_click: click at "
        "coordinate, or at the current pointer position when coordinate "
        "is omitted. "
        "left_click_drag: drag from coordinate to end_coordinate. "
        "scroll: scroll_direction up/down/left/right, scroll_amount wheel "
        "detents (default 3); coordinate moves the pointer first because "
        "scroll goes to whatever is under the pointer. "
        "type: type text literally (US layout; shift symbols handled; "
        "\"\\n\" = Enter). "
        "key: press a key or combo, xdotool style in text: \"enter\", "
        "\"tab\", \"f5\", \"ctrl+s\", \"alt+f4\", \"ctrl+shift+t\". "
        "wait: pause duration seconds (default 1, max 30) to let the app "
        "react. "
        "resize: resize the screen to coordinate [width, height] "
        "(16..16384); the app window is reconfigured. "
        "screen_info: the screen's size, display name, running app pid, "
        "and the env vars for attaching extra apps from Bash. "
        "spawn: create another screen (text = optional label, "
        "coordinate = optional [width, height]). "
        "kill: tear down a screen and its app (screen = id). "
        "list: show every screen with its size, app pid, and attach env "
        "vars. "
        "Requires a vision-capable model: screenshots are returned as "
        "image content."
    )


def _computer_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "enum": [
                    "screenshot",
                    "launch",
                    "close_app",
                    "mouse_move",
                    "left_click",
                    "right_click",
                    "middle_click",
                    "double_click",
                    "left_click_drag",
                    "scroll",
                    "type",
                    "key",
                    "wait",
                    "resize",
                    "screen_info",
                    "spawn",
                    "kill",
                    "list",
                ],
            },
            "coordinate": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "[x, y] screen pixels; (0, 0) is top-left.",
            },
            "end_coordinate": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Drag destination [x, y].",
            },
            "text": {"type": "string"},
            "scroll_direction": {"enum": ["up", "down", "left", "right"]},
            "scroll_amount": {"type": "integer"},
            "duration": {"type": "number"},
            "screen": {
                "type": "string",
                "description": "Target screen id (from spawn/list).",
            },
        },
        "required": ["action"],
    }

