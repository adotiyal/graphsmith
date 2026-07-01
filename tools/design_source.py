"""
tools/design_source.py — optional EXTERNAL design source for the Design agent (Change 1).

When `state["design_source"]` is set (a local directory OR a git URL), the Design agent
REUSES the existing designs instead of generating three fresh directions and pausing for a
human pick: it reverse-engineers the spec to MATCH an imported HTML mockup, uses that mockup
as the design directly, and skips the 3-directions choice. Absent/unusable → the agent falls
back to its normal generate-from-repo-patterns flow.

A source is "usable" when it contains at least one HTML mockup — either listed by a
`design_manifest.md` (lines like `- screens/home.html`) or, failing that, any `*.html` found
(shallow-first, skipping build/vendor dirs). This is the smallest viable slice: an imported
HTML mockup is the SAME artifact the Design agent already produces, so it plugs straight into
the existing mockup→kit path and every downstream guard.

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
    """True when the source contains at least one HTML mockup."""
    return bool(find_mockups(localdir))


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
