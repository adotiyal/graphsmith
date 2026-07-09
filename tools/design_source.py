"""
tools/design_source.py — optional EXTERNAL design source for the Design agent (Change 1).

When `state["design_source"]` is set (a local directory OR a git URL), the Design agent
REUSES the existing designs instead of generating three fresh directions and pausing for a
human pick: it reverse-engineers the spec to MATCH an imported HTML mockup, uses that mockup
as the design directly, and skips the 3-directions choice. Absent/unusable → the agent falls
back to its normal generate-from-repo-patterns flow.

A source is "usable" (Work item C) when it contains at least one design of ANY kind — an
HTML mockup, a rendered screen IMAGE (.png/.jpg/.webp), or a Storybook/screen STORY
composition (`*.stories.tsx`, or a bare `.tsx/.jsx` under a `stories/screens/preview` dir).
`find_mockups`/`load_primary_mockup` stay HTML-only for back-compat (the imported-HTML path is
byte-for-byte unchanged); `find_screens`/`load_story_excerpts` are the generalized layer, and
`has_usable_designs` now returns True for ANY kind (a stories-only or images-only source is
usable). A `design_manifest.md` may list any kind (lines like `- screens/home.png`); manifest
order wins, else shallowest-first per kind, html before image before story.

An `.html` file only counts as an html SCREEN when it is a FULL document (`<!doctype`/`<html`
in its first 2KB) — a guideline/snippet FRAGMENT card is not a screen of any kind, so it can
never outrank real story compositions on the html-first ordering (the ingestion-side twin of
the "extract the DOCUMENT, not a fence" rule). Story excerpts are STRICTLY capped: the file
that crosses total_cap is trimmed so the returned total never exceeds it.

Everything here is best-effort and NEVER raises — any failure returns None/[] so the agent
degrades to generation. A git URL is shallow-cloned into a temp dir; a local path is used as-is.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_CLONE_TIMEOUT = 120
_MANIFEST_NAMES = ("design_manifest.md", "MANIFEST.md", "manifest.md")
_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "coverage", "__pycache__"}
_MAX_MOCKUPS = 25
_MOCKUP_CHAR_CAP = 200_000

# Work item C — generalized design kinds. HTML mockups keep the existing path; images are
# rendered designed screens (the real design_qa baseline); stories are the AUTHORITATIVE
# compositions (they show the exact component usage the kit must reproduce).
# TOOL-OUTPUT dirs (ecosystem-standard artifact names: Playwright screenshots/reports, Jest
# snapshots, Storybook builds) are never design sources — 138 Playwright baseline PNGs under
# a DS repo's tests/__screenshots__ once flooded the image scan. v2-only: find_screens' SCAN
# skips them, but an explicit design_manifest.md listing still wins (a manifest author knows
# what they're pointing at); find_mockups keeps the legacy skip set (byte-identical).
_TOOL_OUTPUT_DIRS = {"__screenshots__", "__snapshots__", "test-results",
                     "playwright-report", "storybook-static"}
_SKIP_DIRS_V2 = _SKIP_DIRS | _TOOL_OUTPUT_DIRS
_HTML_EXTS = {".html", ".htm"}
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_STORY_DIRS = {"stories", "screens", "preview"}
# A manifest line may reference any screen kind; a bare .tsx/.jsx still only counts as a
# story when it lives under a stories/screens/preview dir (resolved in _screen_kind).
_MANIFEST_SCREEN_RE = re.compile(
    r"(?mi)^\s*[-*]\s+`?([^\s`]+\.(?:html|htm|png|jpe?g|webp|tsx|jsx))`?")


def _looks_like_git_url(src: str) -> bool:
    return src.startswith(("http://", "https://", "git@", "ssh://", "git://")) or src.endswith(".git")


def _clone(url: str) -> "str | None":
    """Shallow-clone a git URL into a fresh temp dir. Returns the dir, or None on failure."""
    dst = tempfile.mkdtemp(prefix="graphsmith-design-")
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", url, dst],
                           capture_output=True, text=True, timeout=_CLONE_TIMEOUT)
        if r.returncode != 0:
            shutil.rmtree(dst, ignore_errors=True)
            return None
        return dst
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        return None


def resolve(src: str) -> "str | None":
    """Return a LOCAL directory for the design source. A local path is returned as-is; a git
    URL is shallow-cloned into a temp dir. Returns None if unresolvable (never raises)."""
    if not src:
        return None
    src = src.strip()
    try:
        p = Path(src).expanduser()
        if p.is_dir():
            return str(p)
    except Exception:
        pass
    if _looks_like_git_url(src):
        return _clone(src)
    return None


def _manifest_files(root: Path) -> list:
    """HTML files explicitly listed in a design manifest, in order (may not all exist)."""
    for name in _MANIFEST_NAMES:
        mp = root / name
        if mp.is_file():
            try:
                text = mp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []
            refs = re.findall(r"(?m)^\s*[-*]\s+`?([^\s`]+\.html)`?", text)
            return [(root / r) for r in refs]
    return []


def find_mockups(localdir: str, limit: int = _MAX_MOCKUPS) -> list:
    """Usable HTML mockups in the source, most-preferred first. A `design_manifest.md` (if
    present) picks and orders them; otherwise every `*.html` is used, shallowest-first,
    skipping build/vendor/hidden dirs. Returns [] when none. Never raises."""
    try:
        root = Path(localdir)
        if not root.is_dir():
            return []
        listed = [f for f in _manifest_files(root) if f.is_file()]
        if listed:
            return [str(f) for f in listed[:limit]]
        out = []
        for p in root.rglob("*.html"):
            parents = p.relative_to(root).parts[:-1]
            if any(part in _SKIP_DIRS or part.startswith(".") for part in parents):
                continue
            out.append(p)
        out.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
        return [str(p) for p in out[:limit]]
    except Exception:
        return []


def has_usable_designs(localdir: str) -> bool:
    """True when the source has at least one usable design of ANY kind — a full-document html
    screen, a rendered screen image, or a story composition (find_screens), OR any html the
    LEGACY path would ingest (find_mockups accepts fragments too, and load_primary_mockup —
    deliberately untouched — is what the agent's imported-HTML mode actually consumes, so a
    fragments-only source is still truthfully "usable"). Extended (Work item C) from the old
    html-only test, so a stories-only or images-only source is now usable."""
    return bool(find_mockups(localdir) or find_screens(localdir))


def load_primary_mockup(localdir: str, cap: int = _MOCKUP_CHAR_CAP) -> "tuple | None":
    """(relative_name, html) for the most-preferred mockup, or None. Never raises."""
    mockups = find_mockups(localdir)
    if not mockups:
        return None
    try:
        p = Path(mockups[0])
        html = p.read_text(encoding="utf-8", errors="replace")[:cap]
        try:
            name = str(p.relative_to(localdir))
        except ValueError:
            name = p.name
        return name, html
    except OSError:
        return None


# ── Work item C: generalized screens (html | image | story) ──────────────────

def _is_html_document(p: Path, prefix_bytes: int = 2048) -> bool:
    """True when an .html/.htm file is a FULL document — case-insensitive `<!doctype` or
    `<html` within the first `prefix_bytes` (only that prefix is read). A guideline/snippet
    FRAGMENT card (a bare <div> partial with no document shell) is not a designed screen —
    as the primary mockup it would beat real story compositions purely on the html-first
    ordering (a live DS-repo footgun). Unreadable → False (skipped). Never raises."""
    try:
        with open(p, "rb") as f:
            head = f.read(prefix_bytes).decode("utf-8", errors="replace").lower()
    except OSError:
        return False
    return "<!doctype" in head or "<html" in head


def _screen_kind(p: Path, root: "Path | None" = None) -> "str | None":
    """The design-source kind of a file: 'html' | 'image' | 'story' | None (not a screen).
    An explicit `*.stories.tsx/.jsx` is always a story; a BARE `.tsx/.jsx` counts as a story
    ONLY when it lives under a stories/screens/preview dir (a random component file is not a
    designed screen). An .html/.htm must be a FULL document (_is_html_document) — a fragment
    is not a screen of ANY kind (deliberately NOT reclassified). find_mockups is untouched:
    the legacy html path still accepts fragments."""
    suf = p.suffix.lower()
    name = p.name.lower()
    if suf in _HTML_EXTS:
        return "html" if _is_html_document(p) else None
    if suf in _IMG_EXTS:
        return "image"
    if name.endswith((".stories.tsx", ".stories.jsx")):
        return "story"
    if suf in (".tsx", ".jsx"):
        try:
            parts = p.relative_to(root).parts if root is not None else p.parts
        except ValueError:
            parts = p.parts
        if any(part.lower() in _STORY_DIRS for part in parts):
            return "story"
    return None


def _manifest_screens(root: Path) -> list:
    """Screen files (ANY kind) explicitly listed in a design manifest, in order (may not all
    exist). Mirrors _manifest_files but matches html/image/story extensions — kept separate so
    find_mockups' html-only manifest behavior stays byte-identical."""
    for name in _MANIFEST_NAMES:
        mp = root / name
        if mp.is_file():
            try:
                text = mp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []
            return [(root / r) for r in _MANIFEST_SCREEN_RE.findall(text)]
    return []


def find_screens(localdir: str, limit: int = _MAX_MOCKUPS) -> list:
    """Typed design entries [{"path": str, "kind": "html"|"image"|"story"}], most-preferred
    first. A design_manifest.md (if present) picks + orders across ALL kinds — an explicit
    listing is never skip-filtered (the author knows what they're pointing at); otherwise
    every screen file is used, ordered html→image→story and shallowest-first within each
    kind, skipping build/vendor/hidden dirs AND tool-output dirs (_TOOL_OUTPUT_DIRS:
    Playwright/Jest/Storybook artifacts are never design sources). Total is capped at
    `limit`. Returns [] when none. Never raises."""
    try:
        root = Path(localdir)
        if not root.is_dir():
            return []
        listed = _manifest_screens(root)
        if listed:
            out = []
            for p in listed:
                if p.is_file():
                    k = _screen_kind(p, root)
                    if k:
                        out.append({"path": str(p), "kind": k})
            if out:
                return out[:limit]
        buckets: dict = {"html": [], "image": [], "story": []}
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            parents = p.relative_to(root).parts[:-1]
            if any(part in _SKIP_DIRS_V2 or part.startswith(".") for part in parents):
                continue
            k = _screen_kind(p, root)
            if k:
                buckets[k].append(p)
        out = []
        for kind in ("html", "image", "story"):
            files = sorted(buckets[kind],
                           key=lambda p: (len(p.relative_to(root).parts), str(p)))
            out.extend({"path": str(p), "kind": kind} for p in files)
        return out[:limit]
    except Exception:
        return []


def load_story_excerpts(localdir: str, per_cap: int = 6000, total_cap: int = 24000) -> list:
    """Story-composition files as (relative_name, text) tuples — each per-file HEAD-capped at
    per_cap (a composition's TOP holds the imports + screen structure that matter), and the
    total across ALL returned texts STRICTLY ≤ total_cap: the file that crosses the boundary
    is TRIMMED so the running total lands on the cap (its head is still the useful part),
    then iteration stops. Returns [] when there are no stories / the dir is bad. Never
    raises."""
    out: list = []
    try:
        root = Path(localdir)
        total = 0
        for s in find_screens(localdir):
            if s["kind"] != "story":
                continue
            p = Path(s["path"])
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:per_cap]
            except OSError:
                continue
            text = text[: total_cap - total]   # strict cap: trim the boundary-crossing file
            try:
                name = str(p.relative_to(root))
            except ValueError:
                name = p.name
            out.append((name, text))
            total += len(text)
            if total >= total_cap:
                break
    except Exception:
        return out
    return out
