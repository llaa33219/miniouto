"""Register the file/bash tools as coreouto tools.

Tools are available to the subagent preset by default. outo keeps only
`call_subagent` so it cannot directly touch the filesystem — it must
delegate.
"""

from __future__ import annotations

import coreouto as co

from .bash import bash
from .delete import delete
from .edit import edit
from .write import write


def register_all() -> None:
    """Register Write, Edit, Delete, Bash as coreouto tools.

    Idempotent: if a name is already registered, leave it alone.
    """

    _register_if_missing("Write", _write_handler, _write_schema(), _write_description())
    _register_if_missing("Edit", _edit_handler, _edit_schema(), _edit_description())
    _register_if_missing("Delete", _delete_handler, _delete_schema(), _delete_description())
    _register_if_missing("Bash", _bash_handler, _bash_schema(), _bash_description())


def _register_if_missing(name: str, handler, schema: dict, description: str) -> None:
    if co.get_tool(name) is not None:
        return
    co.register_tool(name, description=description)(handler)


def _write_handler(file_path: str, content: str) -> str:
    return write(file_path, content)


def _edit_handler(file_path: str, edits: list[dict[str, str]]) -> str:
    return edit(file_path, edits)


def _delete_handler(file_path: str) -> str:
    return delete(file_path)


async def _bash_handler(command: str, timeout_seconds: int = 60, cwd: str | None = None) -> str:
    return await bash(command, timeout_seconds=timeout_seconds, cwd=cwd)


def _write_description() -> str:
    return (
        "Create a new file with the given content. Refuses to overwrite "
        "an existing file — use the Edit tool for changes to existing files. "
        "Parent directories are created automatically. Pass an absolute path, "
        "or a path relative to the directory miniouto was invoked from."
    )


def _write_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path of the file to create."},
            "content": {"type": "string", "description": "Full file content to write."},
        },
        "required": ["file_path", "content"],
    }


def _edit_description() -> str:
    return (
        "Apply one or more search/replace edits to a file. Each edit has "
        "oldText (the exact string to find) and newText (its replacement). "
        "Multiple edits in one call all match against the original file; "
        "they cannot overlap. oldText must be unique within the file unless "
        "more context is provided. Pass an absolute path, or a path relative "
        "to the directory miniouto was invoked from."
    )


def _edit_schema() -> dict:
    return {
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
                        "newText": {"type": "string", "description": "Replacement text."},
                    },
                    "required": ["oldText", "newText"],
                },
            },
        },
        "required": ["file_path", "edits"],
    }


def _delete_description() -> str:
    return (
        "Delete a file or an empty directory. Refuses to delete a non-empty "
        "directory — use Bash with `rm -rf` if you really mean it. Pass an "
        "absolute path, or a path relative to the directory miniouto was "
        "invoked from."
    )


def _delete_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to delete."},
        },
        "required": ["file_path"],
    }


def _bash_description() -> str:
    return (
        "Run a shell command. Captures stdout and stderr; exits with the "
        "command's exit code. Default timeout 60s, max 600s. Output >30KB "
        "is truncated with a note. Default cwd is the directory miniouto "
        "was invoked from. Use this for `git`, `grep`, `find`, `ls`, `cat`, "
        "`pytest`, package managers, etc."
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
