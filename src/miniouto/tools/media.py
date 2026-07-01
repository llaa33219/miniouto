"""Media viewing tools: load image / video / audio files for the LLM to perceive.

These tools do NOT return text. They read the file's raw bytes and hand back
a `LoadedMedia` record; the registry layer (tools/registry.py) wraps that into
coreouto `ContentBlock`s (ImageBlock / VideoBlock / AudioBlock) so the model
actually sees or hears the content.

Layer rule: this module is pure stdlib. It must not import coreouto — only
`tools/registry.py` is allowed to. The ContentBlock construction lives there.

Size caps exist because multimodal payloads are uploaded verbatim to the
provider: a 200 MB video would blow the request budget (and most providers
reject it well before that). When a file exceeds the cap, the tool raises
`MediaViewError` with a redirect to Bash-based alternatives (ffmpeg/sox to
downsample, or splitting the media into chunks).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..paths_runtime import INVOCATION_CWD

# Provider-facing payload caps. These are deliberately conservative — below
# Anthropic's hard limits (image 30 MB, document 32 MB) so a single tool call
# can never trip the provider's request-size rejection. Tune upward only if
# you have confirmed your active provider accepts larger inline payloads.
MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 20 MB
MAX_VIDEO_BYTES = 50 * 1024 * 1024   # 50 MB
MAX_AUDIO_BYTES = 25 * 1024 * 1024   # 25 MB

# Extension → MIME, per coreouto's supported block types
# (https://github.com/llaa33219/coreouto/blob/main/docs/tools.md).
# Adding an extension here that the active provider does not understand will
# surface as a provider-side ValueError at call time — keep this in sync with
# coreouto's documented per-format support.
_IMAGE_MIME: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}
_VIDEO_MIME: dict[str, str] = {
    ".mp4":  "video/mp4",
    ".mov":  "video/quicktime",
    ".webm": "video/webm",
}
_AUDIO_MIME: dict[str, str] = {
    ".wav":  "audio/wav",
    ".mp3":  "audio/mpeg",
}


@dataclass
class LoadedMedia:
    """A media file loaded and validated, ready to be wrapped into a ContentBlock.

    `kind` is one of "image" | "video" | "audio" and is carried separately from
    `mime_type` so the registry layer can dispatch to the right block type
    without re-parsing the MIME string.
    """

    path: Path
    data: bytes
    mime_type: str
    kind: str


def load_image(file_path: str) -> LoadedMedia:
    return _load(file_path, "image", _IMAGE_MIME, MAX_IMAGE_BYTES)


def load_video(file_path: str) -> LoadedMedia:
    return _load(file_path, "video", _VIDEO_MIME, MAX_VIDEO_BYTES)


def load_audio(file_path: str) -> LoadedMedia:
    return _load(file_path, "audio", _AUDIO_MIME, MAX_AUDIO_BYTES)


def _load(
    file_path: str,
    kind: str,
    mime_table: dict[str, str],
    max_bytes: int,
) -> LoadedMedia:
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        raise MediaViewError(
            f"file_path is required and must be a non-empty string, got "
            f"{file_path!r}. Pass an absolute path, or a path relative to "
            "the directory miniouto was invoked from."
        )

    path = Path(file_path)
    if not path.is_absolute():
        path = (INVOCATION_CWD / path).resolve()

    if not path.exists():
        raise MediaViewError(f"Path not found: {path}")
    if path.is_dir():
        raise MediaViewError(
            f"{path} is a directory, not a {kind} file. Pass a file path."
        )

    suffix = path.suffix.lower()
    mime_type = mime_table.get(suffix)
    if mime_type is None:
        supported = ", ".join(sorted(mime_table))
        raise MediaViewError(
            f"Unsupported {kind} extension {suffix!r} for {path}. "
            f"Supported extensions: {supported}. To read an unsupported "
            f"format, convert it first with Bash (e.g. ffmpeg for video/"
            f"audio, ImageMagick/Pillow for images)."
        )

    size = path.stat().st_size
    if size > max_bytes:
        mb = size / (1024 * 1024)
        cap_mb = max_bytes / (1024 * 1024)
        raise MediaViewError(
            f"{kind.capitalize()} at {path} is {mb:.1f} MB, which exceeds "
            f"the {cap_mb:.0f} MB cap of the {kind.capitalize()} tool. "
            "Multimodal payloads are uploaded verbatim to the provider and "
            "a file this size would blow the request budget or be rejected. "
            "Downsample first with Bash (e.g. `ffmpeg -i in.mp4 -b:v 1M "
            f"out.mp4` for video, `sox in.wav -r 16000 out.wav` for audio, "
            "`convert in.png -resize 50% out.png` for images) and view the "
            "smaller result."
        )
    if size == 0:
        raise MediaViewError(f"{path} is empty (0 bytes).")

    data = path.read_bytes()
    return LoadedMedia(path=path, data=data, mime_type=mime_type, kind=kind)


class MediaViewError(Exception):
    pass
