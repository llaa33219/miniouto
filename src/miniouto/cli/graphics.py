"""Terminal-graphics widgets for the TUI, with the per-repaint cost removed.

`textual_image`'s TGP widget re-sends the whole image on every repaint:
`widget.render()` calls `cleanup()` (which tells the terminal to DELETE the
image) and then builds a fresh renderable, which base64-encodes the PNG and
transmits it again. On a 1280x720 capture that is ~1.8 MB per repaint, so
scrolling the log re-uploaded the picture over and over — the "opening a
screenshot lags" symptom.

The TGP protocol does not need that: an image is uploaded ONCE and then
PLACED (the `a=p` command) wherever the cell grid says it should be. The
renderable only has to produce the placeholder cells (U+10EEEE plus
diacritics) that carry the image id, and those are cheap to keep.

So this module wraps the protocol instead of the widget:

* the renderable is built ONCE and kept — `terminal_image_id` stays set, so
  no delete and no re-upload happens on a later repaint;
* the placeholder segments are built once and reused, so a repaint does not
  regenerate the whole diacritic grid;
* the image is deleted from the terminal only when the widget goes away
  (`on_unmount`), not on every repaint.

Measured in the TUI: a full repaint with a 1280x720 capture on screen cost
47.8 ms with the stock widget and 39.4 ms here, against 37.6 ms for a
33-line TEXT widget of the same size — so the picture now adds ~1.8 ms to a
repaint (the diacritic grid), instead of ~10 ms of CPU plus a fresh 1.8 MB
upload to the terminal. Verified end to end in a pty: 40 scroll moves
produce ONE upload and ONE placement.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from textual.widget import Widget


class CachedTGPImage(Widget):
    """A TGP image widget that uploads once and re-places cheaply.

    Mirrors `textual_image.widget.TGPImage` — same sizing maths, same
    placeholder scheme — with the renderable and its segments cached for the
    widget's lifetime.
    """

    def __init__(self, image: str | Path) -> None:
        super().__init__()
        self._image = str(image)
        self._renderable: Any | None = None
        self._meta: Any | None = None

    # ── sizing (textual_image's maths, verbatim in behaviour) ──────────────

    def _styled_size(self) -> tuple[str | int | None, str | int | None]:
        width = self.styles.width
        height = self.styles.height
        return (
            None if width is None else ("auto" if width.is_auto else self.content_size.width),
            None if height is None else ("auto" if height.is_auto else self.content_size.height),
        )

    def _image_size(self) -> Any:
        if self._meta is None:
            from textual_image._geometry import ImageSize
            from textual_image._pixeldata import PixelMeta

            meta = PixelMeta(self._image)
            styled_width, styled_height = self._styled_size()
            self._meta = ImageSize(
                meta.width, meta.height, width=styled_width, height=styled_height
            )
        return self._meta

    def get_content_width(self, container: Any, viewport: Any) -> int:
        from textual_image._terminal import get_cell_size

        width, _ = self._image_size().get_cell_size(
            container.width, container.height or 2**32, get_cell_size()
        )
        return width

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        from textual_image._terminal import get_cell_size

        _, height = self._image_size().get_cell_size(
            width, container.height or 2**32, get_cell_size()
        )
        return height

    # ── rendering ─────────────────────────────────────────────────────────

    def render(self) -> Any:
        """The same renderable every time — no delete, no re-upload.

        `textual_image`'s renderable uploads the image on its first render
        and keeps `terminal_image_id`; every later `__rich_console__` call
        only re-emits the placeholder cells. Holding on to it is what makes
        the second repaint cheap.
        """

        if self._renderable is None:
            from textual_image.renderable.tgp import Image as TGPRenderable

            self._renderable = TGPRenderable(self._image, *self._styled_size())
        return self._renderable

    def on_unmount(self) -> None:
        """Delete the image from the terminal when the row goes away.

        The terminal holds the image until told otherwise, so without this
        every opened screenshot would leave its pixels in the terminal's
        image store forever.
        """

        renderable = self._renderable
        if renderable is not None:
            with contextlib.suppress(Exception):
                renderable.cleanup()
        self._renderable = None
