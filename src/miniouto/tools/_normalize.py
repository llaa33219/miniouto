"""Text normalization for the fuzzy fallback in Edit's oldText matching.

The exact-match path uses the raw string. The fallback path normalizes
both `oldText` and the file's contents in lockstep so that harmless
encoding differences (smart quotes pasted from a doc, NBSP from a
browser, CRLF line endings, etc.) don't break the edit.
"""

from __future__ import annotations

import unicodedata

SMART_QUOTE_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
)

DASH_MAP = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)

NBSP = "\u00a0"
ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\ufeff")
BOM = "\ufeff"


def _strip_zero_width(s: str) -> str:
    return s.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")


def normalize_for_matching(s: str) -> str:
    """Apply all fuzzy normalizations. The same function is applied to both
    `oldText` and the file content before retry, so the comparison is fair."""

    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.translate(SMART_QUOTE_MAP)
    s = s.translate(DASH_MAP)
    s = s.replace(NBSP, " ")
    s = _strip_zero_width(s)
    s = unicodedata.normalize("NFKC", s)
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return s


def first_diff_index(a: str, b: str) -> int:
    """Return the index of the first differing character; len(a) if a is a prefix of b."""

    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def find_occurrences(haystack: str, needle: str) -> list[int]:
    """All start indices where needle appears in haystack. Empty needle returns []."""

    if not needle:
        return []
    out: list[int] = []
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return out
        out.append(idx)
        start = idx + 1
