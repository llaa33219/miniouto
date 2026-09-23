"""Style documents: markdown files in ~/miniouto/style/<name>.md."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import toml_io
from .paths import BUNDLED_STYLE_DIR, STYLE_DIR, STYLE_REPOS_FILE, ensure_dirs


@dataclass(frozen=True)
class SubagentSpec:
    """One named subagent declared by a style document.

    ``name`` is the raw tag name (e.g. ``"editor"``, ``"file-picker"``) and is
    used both as the ``name`` argument of the ``call_subagent`` tool and in
    actor labels like ``editor-abc123``.
    """

    name: str
    prompt: str


_PAIR_RE = re.compile(r"<([A-Za-z][A-Za-z0-9_-]*)>(.*?)</\1>", re.DOTALL)


def _top_level_pairs(text: str) -> list[tuple[str, str]]:
    """Find non-overlapping ``<tag>...</tag>`` pairs in document order.

    Sequential scan: each match is accepted, then scanning resumes after its
    closing tag. A tag pair nested inside a body is *content* of that body,
    never a separate pair. The reserved name ``outo`` is always skipped (it
    belongs to the outo prompt, never to a subagent).
    """

    pairs: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _PAIR_RE.search(text, pos)
        if not m:
            break
        if m.group(1) != "outo":
            pairs.append((m.group(1), m.group(2)))
        pos = m.end()
    return pairs


def parse_style(content: str) -> tuple[str, list[SubagentSpec]]:
    """Parse a style document into ``(outo_prompt, subagent_specs)``.

    Format:

    * ``<outo>...</outo>`` is required (or the whole document is the outo
      prompt — preserves the old ``split_style`` fallback behaviour).
    * Every other top-level ``<tag>...</tag>`` pair outside the outo block
      becomes one ``SubagentSpec``; the tag name IS the subagent name.
    * Tag names match ``[A-Za-z][A-Za-z0-9_-]*`` (hyphens allowed: e.g.
      ``file-picker``, ``code-searcher``).
    * ``outo`` is reserved — never appears as a subagent name.
    * Legacy compat: ``<subagent>...</subagent>`` becomes a subagent named
      ``"subagent"`` (treated as any other tag).
    * Duplicate subagent names raise ``ValueError``.
    * Stray prose outside any tag is ignored.
    """

    outo_match = _PAIR_RE.search(content)
    if outo_match is not None and outo_match.group(1) == "outo":
        outo_prompt = outo_match.group(2).strip()
        remainder = content[: outo_match.start()] + content[outo_match.end() :]
    else:
        outo_prompt = content.strip()
        remainder = ""

    specs: list[SubagentSpec] = []
    seen: set[str] = set()
    for tag, body in _top_level_pairs(remainder):
        if tag in seen:
            raise ValueError(f"Duplicate subagent name: {tag!r}")
        seen.add(tag)
        specs.append(SubagentSpec(name=tag, prompt=body.strip()))

    return outo_prompt, specs


def list_styles() -> list[str]:
    ensure_dirs()
    return sorted(p.stem for p in STYLE_DIR.glob("*.md"))


def bundled_style_names() -> list[str]:
    """Return the stems of the bundled templates shipped with the package."""

    if not BUNDLED_STYLE_DIR.is_dir():
        return []
    return sorted(p.stem for p in BUNDLED_STYLE_DIR.glob("*.md"))


def read(name: str) -> str | None:
    path = STYLE_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def path_for(name: str) -> Path:
    return STYLE_DIR / f"{name}.md"


def write(name: str, content: str, *, overwrite: bool = False) -> Path:
    ensure_dirs()
    target = path_for(name)
    if target.exists() and not overwrite:
        return target
    target.write_text(content, encoding="utf-8")
    return target


def add_from_repo(repo_url: str, *, name_override: str | None = None) -> list[str]:
    """Clone a repo's /style-md/ directory contents into the local style dir.

    Files with matching basenames are treated as the same document
    (overwritten in place). Returns the list of style names added/updated.
    The repo URL is recorded so ``style update`` can re-fetch it later.
    """

    ensure_dirs()
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

    candidates = [
        f"{repo_url.rstrip('/')}/style-md/",
        f"{repo_url.rstrip('/')}/tree/main/style-md/",
        f"{repo_url.rstrip('/')}/tree/master/style-md/",
    ]
    last_error: Exception | None = None
    files: dict[str, bytes] = {}
    for url in candidates:
        try:
            files = _fetch_dir(url)
        except Exception as exc:
            last_error = exc
            continue
        if files:
            break
    if not files:
        raise RuntimeError(
            f"Could not find /style-md/ under {repo_url}: {last_error}"
        )

    written: list[str] = []
    for fname, content in files.items():
        if not fname.endswith(".md"):
            continue
        style_name = name_override or fname[: -len(".md")]
        target = write(style_name, content.decode("utf-8", errors="replace"), overwrite=True)
        written.append(target.stem)
    record_repo(repo_url)
    return written


def record_repo(repo_url: str) -> None:
    """Append a repo URL to the tracked style-repo list (deduped, ordered)."""

    ensure_dirs()
    repos = list_repos()
    if repo_url in repos:
        return
    repos.append(repo_url)
    toml_io.save(STYLE_REPOS_FILE, {"repos": repos})


def list_repos() -> list[str]:
    """Return the list of repo URLs previously added via ``style add``."""

    if not STYLE_REPOS_FILE.exists():
        return []
    data = toml_io.load(STYLE_REPOS_FILE)
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        return []
    return [str(r) for r in repos if isinstance(r, str)]


def _fetch_dir(url: str) -> dict[str, bytes]:
    """Try to fetch a directory listing from a few common git hosts."""

    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if host in ("github.com", "www.github.com"):
        return _fetch_github_tree(parsed)
    if host in ("gitlab.com", "www.gitlab.com"):
        return _fetch_gitlab_tree(parsed)
    return _fetch_raw_index(url)


def _fetch_github_tree(parsed) -> dict[str, bytes]:
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL must be /<owner>/<repo>")
    owner, repo = parts[0], parts[1]
    branch = "main"
    if len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]

    api = f"https://api.github.com/repos/{owner}/{repo}/contents/style-md?ref={branch}"
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        r = client.get(api, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        entries = r.json()
    files: dict[str, bytes] = {}
    for entry in entries:
        if entry.get("type") != "file":
            continue
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            blob = client.get(entry["download_url"])
            blob.raise_for_status()
        files[entry["name"]] = blob.content
    return files


def _fetch_gitlab_tree(parsed) -> dict[str, bytes]:
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitLab URL must be /<owner>/<repo>")
    encoded = "%2F".join(parts[:2])
    branch = "main"
    if len(parts) >= 4 and parts[2] == "-":
        branch = parts[3]
    api = f"https://gitlab.com/api/v4/projects/{encoded}/repository/tree?path=style-md&ref={branch}&per_page=100"
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        r = client.get(api)
        r.raise_for_status()
        entries = r.json()
    files: dict[str, bytes] = {}
    for entry in entries:
        if entry.get("type") != "blob":
            continue
        file_path = entry["path"]
        raw = f"https://gitlab.com/api/v4/projects/{encoded}/repository/files/{file_path.split('/')[-1]}/raw?ref={branch}"
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            blob = client.get(raw)
            blob.raise_for_status()
        files[entry["name"]] = blob.content
    return files


def _fetch_raw_index(url: str) -> dict[str, bytes]:
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
    if "<a href=" not in r.text.lower():
        raise ValueError(f"URL did not look like a directory index: {url}")
    from html.parser import HTMLParser

    class HrefParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.hrefs: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag != "a":
                return
            for k, v in attrs:
                if k == "href" and v and v.endswith(".md"):
                    self.hrefs.append(v)

    parser = HrefParser()
    parser.feed(r.text)
    base = url.rstrip("/") + "/"
    files: dict[str, bytes] = {}
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for href in parser.hrefs:
            with client.stream("GET", base + href) as resp:
                resp.raise_for_status()
                files[href] = resp.read()
    return files


def builtin_default() -> str:
    """Return the path to the bundled default style (copied on first run)."""

    bundled = Path(__file__).parent.parent / "default_style" / "default.md"
    target = path_for("default")
    if not target.exists() and bundled.exists():
        ensure_dirs()
        shutil.copyfile(bundled, target)
    return target.read_text(encoding="utf-8") if target.exists() else ""


def write_default_style(content: str) -> None:
    ensure_dirs()
    target = path_for("default")
    if not target.exists():
        target.write_text(content, encoding="utf-8")
