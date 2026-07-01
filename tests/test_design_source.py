"""
External design source (Change 1) — tools/design_source.py + the Design agent reuse branch.

The Design agent, when state["design_source"] points at a local dir or git URL of HTML
mockups, REUSES the imported design: it writes a spec that matches the mockup, uses that
mockup AS the design, and SKIPS the 3-directions human pick. Absent/unusable → normal flow.

The pure module (resolve/clone/find_mockups) is tested deterministically (clone via a mocked
subprocess); the agent branch is tested end to end with the MockLLM (no pause, imported mockup
becomes mockup.html, no A/B/C mockups generated).
"""

import subprocess
from pathlib import Path

from conftest import base_state, seed
from tools import design_source as ds


def _mk(p: Path, content: str = "<html></html>") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── resolve / clone ──────────────────────────────────────────────────────────

def test_resolve_local_dir_returned_as_is(tmp_path):
    assert ds.resolve(str(tmp_path)) == str(tmp_path)


def test_resolve_none_for_missing_nonurl(tmp_path):
    assert ds.resolve(str(tmp_path / "nope")) is None
    assert ds.resolve("") is None


def test_looks_like_git_url():
    assert ds._looks_like_git_url("https://github.com/x/y.git")
    assert ds._looks_like_git_url("git@github.com:x/y.git")
    assert ds._looks_like_git_url("https://github.com/x/y")
    assert not ds._looks_like_git_url("/local/path")


def test_resolve_clones_git_url(tmp_path, monkeypatch):
    cloned = {}
    def fake_run(cmd, capture_output, text, timeout):
        # `git clone --depth 1 <url> <dst>` — create the dst so it looks cloned
        dst = cmd[-1]
        Path(dst).mkdir(parents=True, exist_ok=True)
        _mk(Path(dst) / "home.html", "<h1>cloned</h1>")
        cloned["cmd"] = cmd
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    out = ds.resolve("https://github.com/acme/designs.git")
    assert out and Path(out).is_dir()
    assert cloned["cmd"][:3] == ["git", "clone", "--depth"]
    assert ds.has_usable_designs(out)


def test_resolve_clone_failure_returns_none(tmp_path, monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        class R: returncode = 1; stdout = ""; stderr = "boom"
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ds.resolve("https://github.com/acme/nope.git") is None


# ── find_mockups / manifest / skip-dirs ──────────────────────────────────────

def test_find_mockups_empty_when_none(tmp_path):
    assert ds.find_mockups(str(tmp_path)) == []
    assert ds.has_usable_designs(str(tmp_path)) is False


def test_find_mockups_shallowest_first_and_skips_vendor(tmp_path):
    _mk(tmp_path / "deep" / "nested" / "a.html")
    _mk(tmp_path / "home.html")
    _mk(tmp_path / "node_modules" / "junk.html")     # vendor → skipped
    _mk(tmp_path / ".git" / "x.html")                # hidden → skipped
    found = [Path(f).name for f in ds.find_mockups(str(tmp_path))]
    assert found[0] == "home.html"                   # shallowest first
    assert "junk.html" not in found and "x.html" not in found
    assert "a.html" in found


def test_manifest_picks_and_orders(tmp_path):
    _mk(tmp_path / "screens" / "second.html")
    _mk(tmp_path / "screens" / "first.html")
    _mk(tmp_path / "ignored.html")
    (tmp_path / "design_manifest.md").write_text(
        "# Designs\n- screens/first.html\n- screens/second.html\n", encoding="utf-8")
    got = [Path(f).name for f in ds.find_mockups(str(tmp_path))]
    assert got == ["first.html", "second.html"]      # manifest order, `ignored.html` excluded


def test_load_primary_mockup(tmp_path):
    _mk(tmp_path / "home.html", "<h1>Imported Home</h1>")
    name, html = ds.load_primary_mockup(str(tmp_path))
    assert name == "home.html" and "Imported Home" in html
    assert ds.load_primary_mockup(str(tmp_path / "empty")) is None


def test_never_raises_on_junk():
    assert ds.resolve(None) is None
    assert ds.find_mockups("/no/such/dir") == []
    assert ds.load_primary_mockup("/no/such/dir") is None


# ── Design agent reuse branch (end to end, MockLLM) ──────────────────────────

def test_design_reuses_imported_source_and_skips_pick(llm, ws, tmp_path):
    src = tmp_path / "designrepo"
    _mk(src / "home.html",
        "<!doctype html><html><body><h1>Imported Home</h1>"
        "<button data-testid='cta'>Go</button></body></html>")
    prd = seed(ws, "proj", "prd", "prd.md", "AC-1 (ui): show the home screen")
    llm.default = ("## Design Context\nImported design.\n## Content & Microcopy\n\"Go\"\n"
                   "## Design System\ntokens\n## Flagged Items\nNone.")
    from agents import design
    # detected_stack set (non-react) → skips the stack question AND the kit build,
    # keeping the test focused on the reuse/skip-the-pick behavior.
    out = design.run(base_state(project_id="proj", prd_path=prd,
                                design_source=str(src), detected_stack="Django + HTMX"))

    assert "_clarify" not in out                       # NO human direction-pick pause
    assert out["design_choice"] == "imported"
    assert out["design_mockup_path"].endswith("mockup.html")

    design_dir = ws / "proj" / "design"
    assert "Imported Home" in (design_dir / "mockup.html").read_text()   # the import IS the mockup
    assert not (design_dir / "mockup_A.html").exists()                   # no 3-direction mockups
    assert "## Design Source" in (design_dir / "design_spec.md").read_text()  # provenance recorded


def test_no_design_source_falls_through(tmp_path):
    # unset / empty / unusable source → the reuse branch is not taken
    from agents.design import _resolve_design_source
    assert _resolve_design_source(base_state()) is None
    assert _resolve_design_source(base_state(design_source="")) is None
    assert _resolve_design_source(base_state(design_source=str(tmp_path))) is None  # empty dir, no html
