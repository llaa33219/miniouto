"""Gate: the TUI package's ``pt`` name must be the compat shim, never pyratatui.

``from . import pt`` inside the layer modules resolves through the *package*
attribute ``miniouto.cli.tui.pt``. The package's fallback ``__getattr__`` scans
the layer modules in order, and ``render.py`` re-exports a ``pt`` name, so an
explicit binding of ``pt`` to the ``.pt`` submodule is what keeps the swap from
silently reverting to real pyratatui. That binding is unused at the package
level, so nothing imports a symbol from it — only these assertions fail if it
is ever dropped or moved below the fallback.

Three separate facts are locked here:

1. **The package attribute is the shim.** Resolution is by ``__name__``, which
   no other module can satisfy.
2. **Every layer binds the same module.** A layer reaching into its own
   namespace would paint with types the package attribute never sees.
3. **No layer still reaches for the old substrate.** An ``import pyratatui``
   left behind in a module bypasses the package attribute entirely, so the
   binding above would be green while that module rendered with the old
   library. The scan catches that independently of import order.
"""

from __future__ import annotations

import ast
from pathlib import Path

import miniouto.cli.tui as tui_package
from miniouto.cli.tui import app as app_module
from miniouto.cli.tui import modals as modals_module
from miniouto.cli.tui import nodes as nodes_module
from miniouto.cli.tui import pt as package_pt
from miniouto.cli.tui import render as render_module
from miniouto.cli.tui import rows as rows_module
from miniouto.cli.tui import theme as theme_module
from miniouto.cli.tui import widgets as widgets_module

TUI_DIR = Path(tui_package.__file__).resolve().parent
"""The actual source directory of the package under test."""


def test_package_pt_attribute_is_the_shim_module() -> None:
    # Given/When: the name resolves through the package (what `from . import pt` uses)
    # Then: it is the shim, not whatever the fallback scan happened to find
    assert package_pt.__name__ == "miniouto.cli.tui.pt"
    assert package_pt is tui_package.pt


def test_layer_modules_bind_the_shim_module() -> None:
    # Given: every layer that paints with pt
    layers = {
        "theme": theme_module,
        "render": render_module,
        "rows": rows_module,
        "widgets": widgets_module,
        "modals": modals_module,
        "nodes": nodes_module,
        "app": app_module,
    }

    # When/Then: each one's `pt` is the shim, so a frame is built from shim types
    for name, layer in layers.items():
        assert layer.pt is package_pt, f"{name}.py still binds a foreign pt"


def test_no_tui_layer_imports_pyratatui() -> None:
    # Given: every module in the package
    sources = sorted(TUI_DIR.glob("*.py"))
    assert sources, "no TUI modules found"

    # When: each import statement is inspected
    offenders: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "pyratatui" for name in names):
                offenders.append(f"{path.name}:{node.lineno}")

    # Then: the old substrate is gone from the package
    assert offenders == []
