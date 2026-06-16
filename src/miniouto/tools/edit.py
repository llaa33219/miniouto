"""Edit tool: precise search/replace with exact-match priority and fuzzy fallback.

Implements the six rules from the spec:

1. Exact match required; smart fallback only on encoding differences.
2. Uniqueness enforced; ambiguous edits rejected with line numbers.
3. Multiple edits in one call, matched against the original (not incrementally).
4. Overlapping edits rejected.
5. Empty oldText and no-op edits rejected.
6. Error messages include line numbers and how-to-fix guidance.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

from ..paths_runtime import INVOCATION_CWD
from ._normalize import find_occurrences, normalize_for_matching


def edit(
    file_path: str,
    edits: list[dict[str, str]],
    *,
    encoding: str = "utf-8",
) -> str:
    """Apply a batch of search/replace edits to a file.

    Each edit dict has `oldText` and `newText`. All edits are matched against
    the original file content; their replacements are then applied in a single
    pass. Overlapping edits are rejected before any write happens.
    """

    if not edits:
        raise EditError("No edits provided. Pass at least one {oldText, newText} pair.")

    path = Path(file_path)
    if not path.is_absolute():
        path = (INVOCATION_CWD / path).resolve()
    if not path.exists():
        raise EditError(
            f"File not found: {path}. Use the Write tool to create a new file.",
            file_path=str(path),
        )
    if path.is_dir():
        raise EditError(f"Path is a directory, not a file: {path}", file_path=str(path))

    try:
        original = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        original = path.read_text(encoding=encoding, errors="replace")

    for i, e in enumerate(edits):
        _validate_edit(i, e)

    spans: list[tuple[int, int, int, str, str]] = []
    content_for_match = original
    for i, e in enumerate(edits):
        old, new = e["oldText"], e["newText"]
        try:
            span = _locate_unique(content_for_match, old, edit_index=i, file_path=str(path))
        except EditError:
            span = _locate_unique_fuzzy(
                content_for_match, old, edit_index=i, file_path=str(path)
            )
        spans.append((i, span[0], span[1], old, new))

    _check_no_overlaps(spans, str(path))

    for _i, start, end, _old, new in spans:
        original = original[:start] + new + original[end:]

    path.write_text(original, encoding=encoding)
    return _summary(spans, str(path), original)


def _validate_edit(index: int, edit: dict[str, Any]) -> None:
    if not isinstance(edit, dict):
        raise EditError(f"edits[{index}] must be an object with 'oldText' and 'newText'.")
    if "oldText" not in edit or "newText" not in edit:
        raise EditError(
            f"edits[{index}] missing required keys. Both 'oldText' and 'newText' are required."
        )
    old, new = edit["oldText"], edit["newText"]
    if not isinstance(old, str) or not isinstance(new, str):
        raise EditError(
            f"edits[{index}]: 'oldText' and 'newText' must be strings, got "
            f"{type(old).__name__} and {type(new).__name__}."
        )
    if old == "":
        raise EditError(
            f"edits[{index}]: 'oldText' is empty. Pass the literal text you want to replace."
        )
    if old == new:
        raise EditError(
            f"edits[{index}]: 'oldText' and 'newText' are identical — no change to make. "
            "Pass a different 'newText' or remove this edit."
        )


def _locate_unique(
    content: str, old_text: str, *, edit_index: int, file_path: str
) -> tuple[int, int]:
    """Locate old_text in content via exact match. Uniqueness required."""

    indices = find_occurrences(content, old_text)
    if not indices:
        raise EditError(_no_match_message(content, old_text, edit_index, file_path))
    if len(indices) > 1:
        lines = _lines_for_indices(content, indices, len(old_text))
        raise EditError(
            f"edits[{edit_index}]: Found {len(indices)} occurrences of oldText. "
            f"The text must be unique. Please provide more context to make it unique. "
            f"Occurrences start at lines: {lines}."
        )
    start = indices[0]
    return start, start + len(old_text)


def _locate_unique_fuzzy(
    content: str, old_text: str, *, edit_index: int, file_path: str
) -> tuple[int, int]:
    """Fallback: normalize both sides in lockstep and try again."""

    norm_content = normalize_for_matching(content)
    norm_old = normalize_for_matching(old_text)
    if norm_old == "":
        raise EditError(
            f"edits[{edit_index}]: 'oldText' is empty after normalization. "
            "Provide actual text to replace."
        )
    indices = find_occurrences(norm_content, norm_old)
    if not indices:
        raise EditError(_no_match_message(content, old_text, edit_index, file_path, fuzzy=True))
    if len(indices) > 1:
        lines = _lines_for_indices(norm_content, indices, len(norm_old))
        raise EditError(
            f"edits[{edit_index}]: Found {len(indices)} occurrences of oldText "
            "even after fuzzy normalization (smart quotes, dashes, whitespace, "
            "line endings). The text must be unique. Please provide more context "
            f"to make it unique. Occurrences start at lines: {lines}."
        )
    start = indices[0]
    return start, start + len(norm_old)


def _check_no_overlaps(
    spans: list[tuple[int, int, int, str, str]], file_path: str
) -> None:
    """All edits matched against the same original; their byte ranges must not overlap."""

    sorted_spans = sorted(spans, key=lambda s: s[1])
    for a, b in pairwise(sorted_spans):
        if a[2] > b[1]:
            raise EditError(
                f"edits[{a[0]}] and edits[{b[0]}] overlap. "
                "Please merge them into one edit. Overlapping ranges: "
                f"edits[{a[0]}]=[{a[1]}, {a[2]}), edits[{b[0]}]=[{b[1]}, {b[2]})."
            )


def _lines_for_indices(content: str, indices: list[int], length: int) -> list[int]:
    """Convert byte offsets to 1-based line numbers for the start of each match."""

    lines: list[int] = []
    for idx in indices:
        line = content.count("\n", 0, idx) + 1
        lines.append(line)
    return lines


def _no_match_message(
    content: str, old_text: str, edit_index: int, file_path: str, fuzzy: bool = False
) -> str:
    snippet = old_text if len(old_text) <= 80 else old_text[:77] + "..."
    if fuzzy:
        return (
            f"edits[{edit_index}]: oldText not found in {file_path}, even after "
            f"normalizing smart quotes, dashes, NBSP, BOM, CRLF, and trailing whitespace. "
            f"oldText was: {snippet!r}. Re-read the file and copy the exact text."
        )
    first_line = old_text.split("\n", 1)[0]
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return (
        f"edits[{edit_index}]: oldText not found in {file_path}. "
        f"First line of oldText: {first_line!r}. "
        "Re-read the file and copy the exact text, including whitespace and indentation."
    )


def _summary(spans: list[tuple[int, int, int, str, str]], file_path: str, new_content: str) -> str:
    n = len(spans)
    parts = [f"Applied {n} edit{'s' if n != 1 else ''} to {file_path}."]
    for i, start, _end, _old, new in spans:
        line = new_content.count("\n", 0, start) + 1
        preview = new.split("\n", 1)[0]
        if len(preview) > 80:
            preview = preview[:77] + "..."
        parts.append(f"  edits[{i}] @ line {line}: now starts with {preview!r}")
    return "\n".join(parts)


class EditError(Exception):
    def __init__(self, message: str, *, file_path: str | None = None) -> None:
        super().__init__(message)
        self.file_path = file_path
