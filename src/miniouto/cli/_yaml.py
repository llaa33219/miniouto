"""The one YAML emitter every listing command shares.

`provider providers/models/list`, `skill list`, `style list`,
`subagent show`, `message list`, and `status` print their result through
`emit_yaml` — plain YAML on stdout and nothing else (no banner, no comment,
no rich markup). That output is the official machine interface for
third-party CLI wrappers, which parse it with `yaml.safe_load`, so the dump
options here are part of the contract:

* ``allow_unicode`` — text stays human-readable;
* ``sort_keys=False`` — the schema's key order is the author's, not alphabetical;
* ``default_flow_style=False`` — block style everywhere, no inline ``{}``;
* ``width=100`` — long text folds at spaces instead of one huge line.

A string that contains newlines (a message body, say) is emitted as a
literal block scalar (`|`), which is the readable form; the default plain
style quotes whatever cannot live unquoted. The dumper is a `SafeDumper`
subclass, so only the representer changes — the output stays safe_load-able.
"""

from __future__ import annotations

from typing import Any

import yaml


class _Dumper(yaml.SafeDumper):
    """SafeDumper with multiline strings as literal block scalars."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _represent_str)


def emit_yaml(data: Any) -> str:
    """Render `data` as the canonical miniouto CLI YAML document."""

    return yaml.dump(
        data,
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )


def print_yaml(data: Any) -> None:
    """Print the canonical YAML document to stdout, and nothing else."""

    import typer

    typer.echo(emit_yaml(data), nl=False)
