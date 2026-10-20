"""Lock the terminal-graphics widgets' per-repaint cost.

`textual_image`'s TGP widget re-uploads the whole image on every repaint:
`render()` calls `cleanup()` (a DELETE to the terminal) and builds a fresh
renderable, which base64-encodes the PNG and transmits it again. On a
1280x720 capture that is ~1.8 MB per repaint, so scrolling the log
re-uploaded the picture over and over — the "opening a screenshot lags"
symptom.

The TGP protocol does not need that: an image is uploaded ONCE and then
PLACED wherever the cell grid says. These tests pin that the widget we ship
uploads once and keeps its image id.
"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from miniouto.cli.graphics import CachedTGPImage

sys.path.insert(0, "tests")
from test_ascii_art import make_png


@pytest.fixture
def shot(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(make_png([[(200, 40, 40, 255)] * 64 for _ in range(36)], 6))
    return path


def test_the_renderable_is_built_once_and_kept(shot) -> None:
    # Given: a TGP image widget
    widget = CachedTGPImage(shot)
    # When: it is rendered twice
    first = widget.render()
    second = widget.render()
    # Then: the SAME renderable comes back — so the image id (and the
    # terminal's copy of the pixels) survives the second render
    assert first is second


def test_the_renderable_keeps_its_terminal_image_id(shot) -> None:
    # Given: a widget whose renderable was actually rendered to a console
    # (the renderable only gets its terminal image id when Rich renders it)
    widget = CachedTGPImage(shot)
    renderable = widget.render()
    console = Console(width=120, height=45, file=io.StringIO(), force_terminal=True)
    list(console.render(renderable, console.options))  # render() is lazy
    assert getattr(renderable, "terminal_image_id", None) is not None

    # When: the widget renders again
    widget.render()
    # Then: the renderable still holds the id, so the next render emits the
    # placement instead of a fresh upload
    assert getattr(renderable, "terminal_image_id", None) is not None


def test_unmounting_deletes_the_image_from_the_terminal(shot) -> None:
    # Given: a widget whose image was uploaded
    widget = CachedTGPImage(shot)
    renderable = widget.render()
    sent: list[object] = []
    renderable.cleanup = lambda: sent.append(renderable)  # type: ignore[method-assign]
    # When: it goes away
    widget.on_unmount()
    # Then: the terminal is told to drop the image
    assert sent == [renderable]


def test_scrolling_does_not_re_upload_the_image(shot) -> None:
    """The end-to-end symptom: scrolling must not re-upload the picture.

    The stock TGP widget deleted and re-sent the whole image on every
    repaint, so scrolling a log with a screenshot on screen pushed ~1.8 MB
    per frame. This drives the real widget the way the TUI does and counts
    the uploads.
    """

    import io

    from rich.console import Console

    widget = CachedTGPImage(shot)
    renderable = widget.render()
    console = Console(width=120, height=45, file=io.StringIO(), force_terminal=True)

    uploads: list[int] = []
    real_send = renderable._send_image_to_terminal

    def counting_send(width, height):
        uploads.append(1)
        real_send(width, height)

    renderable._send_image_to_terminal = counting_send  # type: ignore[method-assign]

    # three renders, as three repaints would do
    for _ in range(3):
        list(console.render(renderable, console.options))

    # Then: the picture was uploaded ONCE
    assert len(uploads) == 1
