"""Register the bash/media tools as coreouto tools."""

from __future__ import annotations

import coreouto as co

from .bash import bash
from .media import load_audio, load_image, load_video


def register_all() -> None:
    """Register Bash, Image, Video, Audio as coreouto tools.

    Idempotent: if a name is already registered, leave it alone.
    """

    _register_if_missing("Bash", _bash_handler, _bash_schema(), _bash_description())
    _register_if_missing("Image", _image_handler, _image_schema(), _image_description())
    _register_if_missing("Video", _video_handler, _video_schema(), _video_description())
    _register_if_missing("Audio", _audio_handler, _audio_schema(), _audio_description())


def _register_if_missing(name: str, handler, schema: dict, description: str) -> None:
    if co.get_tool(name) is not None:
        return
    co.register_tool(name, description=description)(handler)


async def _bash_handler(command: str, timeout_seconds: int = 60, cwd: str | None = None) -> str:
    return await bash(command, timeout_seconds=timeout_seconds, cwd=cwd)


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
        "command's exit code. Default timeout 60s, max 600s. Output >30KB "
        "is truncated with a note. Default cwd is the directory miniouto "
        "was invoked from. This is the ONLY file-manipulation tool: read "
        "with `cat`/`grep`/`find`, create with `cat > file <<'EOF'` or "
        "`tee`, edit with `sed -i` or a short Python snippet, delete with "
        "`rm`. Also use it for `git`, `pytest`, package managers, etc."
    )


def _bash_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute."},
            "timeout_seconds": {
                "type": "integer",
                "description": "Max seconds to wait (default 60, max 600).",
                "minimum": 1,
                "maximum": 600,
            },
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
