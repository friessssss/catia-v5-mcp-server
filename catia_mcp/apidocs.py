"""Search the installation's own automation reference (the CHM help file).

The 3DEXPERIENCE automation API is documented only in the DSYAutomation.chm
shipped with each installation (V5: V5Automation.chm), and members drift
between releases, so the agent should read the user's installed reference,
not a generic one. The CHM is decompiled once with hh.exe (built into
Windows; 7-Zip is a fallback) into the state directory, and a JSON index of
the pages is cached next to it.

The pure functions (build_index, search, page_text, strip_html) need no
CATIA and no Windows, so they are tested offline. Locating the CHM from
Application.FullName and running hh.exe -decompile need live validation on
the 3DEXPERIENCE laptop: the install layout below is the documented
default, not field-proven.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from catia_mcp.errors import InvalidArgument, ObjectNotFound, UnsupportedCapability
from catia_mcp.paths import state_dir

# An interface page lists every member on one page (HybridShapeFactory runs
# to well over 100k characters), so the index keeps nearly all of it: a
# short cap made members deep in large interfaces unsearchable.
INDEX_TEXT_CHARS = 400_000
PAGE_TEXT_CHARS = 30_000
SNIPPET_CHARS = 300
# Versioned so an index built with different settings is rebuilt, not reused.
_INDEX_FILE = "index-v2.json"
_DONE_MARKER = ".extracted"
_KNOWN_CHMS = ("DSYAutomation.chm", "V5Automation.chm")
_HTML_SUFFIXES = {".htm", ".html"}

_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_TAG = re.compile(r"<(br|p|div|tr|li|h[1-6]|dt|dd|table)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


# ── Pure functions (offline-testable) ──


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_html(markup: str) -> str:
    """Readable text of an HTML page: no scripts/styles/tags, entities
    decoded, whitespace collapsed (line breaks kept at block tags)."""
    text = _SCRIPT_STYLE.sub(" ", markup)
    text = _COMMENT.sub(" ", text)
    text = _BLOCK_TAG.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n", text).strip()


def _title(markup: str, fallback: str) -> str:
    match = _TITLE.search(markup)
    if match:
        title = " ".join(html.unescape(_TAG.sub(" ", match.group(1))).split())
        if title:
            return title
    return fallback


def build_index(html_dir: Path) -> list[dict[str, str]]:
    """One entry per HTML page under html_dir: page (relative POSIX path),
    title, text (stripped, truncated to INDEX_TEXT_CHARS)."""
    root = Path(html_dir)
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in _HTML_SUFFIXES or not path.is_file():
            continue
        markup = _decode(path.read_bytes())
        text = strip_html(markup)
        entries.append({
            "page": path.relative_to(root).as_posix(),
            "title": _title(markup, path.stem),
            "text": text[:INDEX_TEXT_CHARS],
        })
    return entries


def _compile(pattern: str) -> re.Pattern[str]:
    if not pattern or not pattern.strip():
        raise InvalidArgument("query must not be empty.")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise InvalidArgument(f"query is not a valid regular expression: {e}") from e


def _snippet(text: str, start: int, end: int) -> str:
    half = max(0, (SNIPPET_CHARS - (end - start)) // 2)
    lo = max(0, start - half)
    hi = min(len(text), end + half)
    snippet = " ".join(text[lo:hi].split())
    return ("..." if lo > 0 else "") + snippet + ("..." if hi < len(text) else "")


def search(index: list[dict[str, str]], pattern: str, max_results: int = 20) -> list[dict[str, str]]:
    """Pages whose title or text match the regex (case-insensitive). Title
    matches rank first, then pages with more text matches."""
    regex = _compile(pattern)
    scored: list[tuple[int, int, int, dict[str, str]]] = []
    for position, entry in enumerate(index):
        title_hit = regex.search(entry["title"]) is not None
        text_match = regex.search(entry["text"])
        if not title_hit and text_match is None:
            continue
        hits = sum(1 for _ in regex.finditer(entry["text"])) if text_match else 0
        if text_match:
            snippet = _snippet(entry["text"], text_match.start(), text_match.end())
        else:
            snippet = _snippet(entry["text"], 0, 0)
        result = {"page": entry["page"], "title": entry["title"], "snippet": snippet}
        scored.append((0 if title_hit else 1, -hits, position, result))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:max_results]]


def resolve_page(index: list[dict[str, str]], page: str) -> str:
    """The indexed page path matching `page`: exact relative path, then
    file name, then title (all case-insensitive)."""
    wanted = page.strip().replace("\\", "/").lower()
    if not wanted:
        raise InvalidArgument("page must not be empty.")
    for key in (
        lambda e: e["page"].lower(),
        lambda e: e["page"].rsplit("/", 1)[-1].lower(),
        lambda e: e["page"].rsplit("/", 1)[-1].rsplit(".", 1)[0].lower(),
        lambda e: e["title"].lower(),
    ):
        for entry in index:
            if key(entry) == wanted:
                return entry["page"]
    raise ObjectNotFound(
        f"No API doc page '{page}'. Use catia_api_search to find page names."
    )


def _page_full_text(html_dir: Path, page: str) -> str:
    """Stripped text of one page, confined to html_dir."""
    root = Path(html_dir).resolve()
    path = (root / page).resolve()
    if root not in path.parents or not path.is_file():
        raise ObjectNotFound(f"No API doc page '{page}'.")
    return strip_html(_decode(path.read_bytes()))


def page_chunk(
    html_dir: Path, page: str, offset: int = 0, limit: int = PAGE_TEXT_CHARS
) -> tuple[str, int, int | None]:
    """(text from offset, total length, next offset or None) of one page."""
    if offset < 0:
        raise InvalidArgument("offset must not be negative.")
    text = _page_full_text(html_dir, page)
    end = offset + limit
    return text[offset:end], len(text), (end if end < len(text) else None)


def page_text(html_dir: Path, page: str, limit: int = PAGE_TEXT_CHARS) -> str:
    """Stripped text of one page (first `limit` characters), confined to html_dir."""
    text = _page_full_text(html_dir, page)
    if len(text) > limit:
        text = text[:limit] + f"\n... ({len(text) - limit} more characters cut)"
    return text


# ── Locating, extracting and caching (Windows, needs live validation) ──


def candidate_dirs(app_full_name: str) -> list[Path]:
    """Directories that may hold the automation CHM, nearest first.

    Application.FullName is the CATIA executable, normally in
    <install>\\win_b64\\code\\bin (3DEXPERIENCE: DSYAutomation.chm there;
    V5: V5Automation.chm in intel_a or win_b64\\code\\bin). Parent
    directories up to the install root are searched too.
    """
    base = Path(app_full_name).parent
    dirs: list[Path] = [base]
    for root in (base.parent, base.parent.parent, base.parent.parent.parent):
        dirs.append(root)
        for sub in ("code/bin", "win_b64/code/bin", "intel_a/code/bin", "doc", "win_b64/doc"):
            dirs.append(root / sub)
    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def find_chm(app_full_name: str | None) -> Path | None:
    """The automation reference CHM: CATIA_MCP_API_CHM, then the known
    names, then any *Automation*.chm near the CATIA executable."""
    override = os.environ.get("CATIA_MCP_API_CHM")
    if override and Path(override).is_file():
        return Path(override)
    if not app_full_name:
        return None
    dirs = [d for d in candidate_dirs(app_full_name) if d.is_dir()]
    for d in dirs:
        for name in _KNOWN_CHMS:
            if (d / name).is_file():
                return d / name
    for d in dirs:
        found = sorted(d.glob("*Automation*.chm"))
        if found:
            return found[0]
    return None


def docs_root() -> Path:
    path = state_dir() / "apidocs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def extraction_dir(chm: Path) -> Path:
    """Per-CHM cache directory: <stem>-<size>, so an updated CHM re-extracts."""
    return docs_root() / f"{chm.stem}-{chm.stat().st_size}"


def _short_path(path: Path) -> str:
    """8.3 form of a path on Windows (hh.exe mishandles quoted paths with
    spaces); the path unchanged elsewhere or when 8.3 names are off."""
    try:
        import win32api

        return str(win32api.GetShortPathName(str(path)))
    except Exception:  # noqa: BLE001 -- not Windows / no 8.3 names
        return str(path)


def _extract_commands(chm: Path, dest: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    hh = shutil.which("hh.exe") or (str(Path(windir) / "hh.exe") if windir else None)
    if hh:
        commands.append([hh, "-decompile", _short_path(dest), _short_path(chm)])
    seven = shutil.which("7z") or shutil.which("7z.exe")
    if seven:
        commands.append([seven, "x", "-y", f"-o{dest}", str(chm)])
    return commands


def _has_html(directory: Path) -> bool:
    return any(p.suffix.lower() in _HTML_SUFFIXES for p in directory.rglob("*"))


_CHM_REF = re.compile(r"([A-Za-z0-9_.\-]+\.chm)", re.IGNORECASE)


def _merged_chms(dest: Path, chm: Path) -> list[Path]:
    """CHMs a master help file merges in ([MERGE FILES] in the .hhp,
    ms-its:X.chm:: links in the contents): Dassault splits large references
    that way, and the master alone then holds no pages."""
    names: set[str] = set()
    for item in dest.rglob("*"):
        if item.suffix.lower() in {".hhp", ".hhc", ".hhk"} and item.is_file():
            try:
                names.update(_CHM_REF.findall(item.read_text(encoding="latin-1")))
            except OSError:
                continue
    found = []
    for name in sorted(names):
        candidate = chm.parent / Path(name).name
        if candidate.is_file() and candidate.resolve() != chm.resolve():
            found.append(candidate)
    return found


def _run_extractors(chm: Path, dest: Path, runner: Callable[..., Any], errors: list[str]) -> bool:
    commands = _extract_commands(chm, dest)
    for command in commands:
        try:
            runner(command, check=False, timeout=600, capture_output=True)
        except Exception as e:  # noqa: BLE001 -- try the next extractor
            errors.append(f"{Path(command[0]).name}: {e}")
            continue
        if _has_html(dest):
            return True
        errors.append(f"{Path(command[0]).name} on {chm.name}: no HTML pages produced")
    return False


def extract_chm(
    chm: Path,
    dest: Path,
    runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """Decompile `chm` into `dest` once. hh.exe's exit code is unreliable,
    so success is judged by HTML files appearing in dest. A master CHM
    without pages of its own pulls in the CHMs it merges."""
    if (dest / _DONE_MARKER).exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    if not _extract_commands(chm, dest):
        raise UnsupportedCapability(
            "Cannot decompile the CATIA automation CHM: neither hh.exe (Windows) nor "
            "7-Zip was found. Set CATIA_MCP_API_CHM or use catia_inspect_api instead."
        )
    errors: list[str] = []
    ok = _run_extractors(chm, dest, runner, errors)
    if not ok:
        for merged in _merged_chms(dest, chm):
            ok = _run_extractors(merged, dest / merged.stem, runner, errors) or ok
    if ok:
        (dest / _DONE_MARKER).write_text(str(chm), encoding="utf-8")
        return dest
    raise UnsupportedCapability(
        f"Could not decompile {chm} ({'; '.join(errors)}). "
        "Use catia_inspect_api to read the live type information instead."
    )


def load_index(html_dir: Path) -> list[dict[str, str]]:
    """The cached index of html_dir, built on first use."""
    cache = Path(html_dir) / _INDEX_FILE
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    index = build_index(html_dir)
    cache.write_text(json.dumps(index), encoding="utf-8")
    return index


def cached_extractions() -> list[Path]:
    """Previously extracted CHMs, newest first (usable without CATIA)."""
    root = docs_root()
    found = [d for d in root.iterdir() if (d / _DONE_MARKER).exists()]
    return sorted(found, key=lambda d: (d / _DONE_MARKER).stat().st_mtime, reverse=True)


class ApiDocs:
    """Lazily locates, extracts and indexes one installation's CHM; falls
    back to an index of CATIA's type libraries (exact member signatures and
    helpstrings, no prose) when the CHM is missing or cannot be unpacked."""

    def __init__(self, runner: Callable[..., Any] = subprocess.run) -> None:
        self.runner = runner
        self.html_dir: Path | None = None
        self.index: list[dict[str, str]] | None = None
        self.source: str | None = None
        self.note: str | None = None

    def ensure(
        self,
        app_full_name: str | None,
        typelibs: Callable[[], list[Any]] | None = None,
    ) -> tuple[Path | None, list[dict[str, str]]]:
        if self.index is not None:
            return self.html_dir, self.index
        try:
            html_dir = self._chm_dir(app_full_name)
        except UnsupportedCapability as error:
            if typelibs is None:
                raise
            from catia_mcp.introspect import typelib_index

            libs = typelibs()
            index = typelib_index(libs)
            if not index:
                raise UnsupportedCapability(
                    f"{error} No CATIA type library could be read either."
                ) from error
            self.html_dir = None
            self.index = index
            self.source = f"CATIA type libraries ({len(libs)} loaded)"
            self.note = f"Help file unavailable, searching type libraries instead: {error}"
            return None, index
        self.html_dir = html_dir
        self.index = load_index(html_dir)
        return html_dir, self.index

    def _chm_dir(self, app_full_name: str | None) -> Path:
        chm = find_chm(app_full_name)
        if chm is not None:
            html_dir = extract_chm(chm, extraction_dir(chm), self.runner)
            self.source = str(chm)
            return html_dir
        cached = cached_extractions()
        if not cached:
            where = (
                f"near {Path(app_full_name).parent}" if app_full_name
                else "(CATIA is not connected, so its install folder is unknown)"
            )
            raise UnsupportedCapability(
                f"No CATIA automation reference (*Automation*.chm) found {where}. "
                "Set CATIA_MCP_API_CHM to the CHM path, or use catia_inspect_api."
            )
        self.source = f"cached extraction {cached[0].name}"
        return cached[0]

    def chunk(self, page: str, offset: int = 0) -> tuple[str, str, str, int, int | None]:
        """(resolved page, title, text from offset, total, next offset)."""
        assert self.index is not None
        resolved = resolve_page(self.index, page)
        entry = next(e for e in self.index if e["page"] == resolved)
        if self.html_dir is not None:
            text, total, next_offset = page_chunk(self.html_dir, resolved, offset)
        else:
            if offset < 0:
                raise InvalidArgument("offset must not be negative.")
            full = entry["text"]
            end = offset + PAGE_TEXT_CHARS
            text, total, next_offset = full[offset:end], len(full), (end if end < len(full) else None)
        return resolved, entry["title"], text, total, next_offset

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"source": self.source, "pages": len(self.index or [])}
        if self.note:
            info["note"] = self.note
        return info
