"""
Design-source v2 (Work item C) — stories + images as design ground truth.

tools/design_source.find_screens/load_story_excerpts generalize the html-only source to
typed screens (html | image | story); the Design agent's imported no-html path reuses them
(spec + kit steered by the STORY compositions, the first screen IMAGE copied as the design_qa
baseline); design_qa compares the live app against that real baseline instead of the
self-generated mockup. All best-effort, never raises, and byte-identical when absent.

The pure module is tested deterministically; the agent branches run end to end with MockLLM.
"""

from pathlib import Path

from conftest import base_state, seed
from tools import design_source as ds


def _mk(p: Path, content: str = "<html></html>") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    return p


# ── find_screens: typed + ordered ────────────────────────────────────────────

def test_find_screens_typed_ordered_and_skips_vendor_hidden(tmp_path):
    _mk(tmp_path / "deep" / "nested" / "a.html")
    _mk(tmp_path / "home.html")
    _png(tmp_path / "shot.png")
    _mk(tmp_path / "stories" / "Home.stories.tsx", "export const Home = () => null")
    _mk(tmp_path / "node_modules" / "junk.html")          # vendor → skipped
    _png(tmp_path / ".hidden" / "x.png")                  # hidden → skipped

    screens = ds.find_screens(str(tmp_path))
    kinds = [s["kind"] for s in screens]
    names = [Path(s["path"]).name for s in screens]
    # html before image before story; within html, shallowest-first
    assert kinds == ["html", "html", "image", "story"]
    assert names == ["home.html", "a.html", "shot.png", "Home.stories.tsx"]
    assert "junk.html" not in names and "x.png" not in names


def test_find_screens_bare_tsx_only_under_story_dir(tmp_path):
    # a bare .tsx counts as a story ONLY under stories/screens/preview
    _mk(tmp_path / "screens" / "Dashboard.tsx", "export default () => null")
    _mk(tmp_path / "components" / "Random.tsx", "export default () => null")  # not a screen
    screens = ds.find_screens(str(tmp_path))
    names = [Path(s["path"]).name for s in screens]
    assert names == ["Dashboard.tsx"]
    assert screens[0]["kind"] == "story"


def test_find_screens_manifest_orders_across_kinds(tmp_path):
    _png(tmp_path / "screens" / "home.png")
    _mk(tmp_path / "screens" / "Home.stories.tsx", "export const Home = () => null")
    _mk(tmp_path / "ignored.html")                        # present but not manifest-listed
    (tmp_path / "design_manifest.md").write_text(
        "# Designs\n- screens/home.png\n- screens/Home.stories.tsx\n", encoding="utf-8")
    screens = ds.find_screens(str(tmp_path))
    assert [(Path(s["path"]).name, s["kind"]) for s in screens] == [
        ("home.png", "image"), ("Home.stories.tsx", "story")]   # manifest order; ignored.html out


def test_find_screens_caps_total(tmp_path):
    for i in range(6):
        _mk(tmp_path / f"s{i}.html")
    assert len(ds.find_screens(str(tmp_path), limit=3)) == 3


def test_find_screens_never_raises_on_junk(tmp_path):
    assert ds.find_screens("/no/such/dir") == []
    assert ds.find_screens(str(tmp_path)) == []           # empty dir


# ── html FRAGMENTS are not screens (full-document rule) ──────────────────────

def test_find_screens_excludes_html_fragments(tmp_path):
    # a guideline FRAGMENT card (no <!doctype/<html> shell) is NOT a screen of any kind;
    # a full document is — case-insensitively, anywhere in the first 2KB.
    _mk(tmp_path / "brand-iconography.card.html",
        "<!-- @dsCard name='Iconography' -->\n<div class='card'>16/20/24 icons</div>")
    _mk(tmp_path / "home.html", "<!DOCTYPE html><head></head><body>Home</body>")
    _mk(tmp_path / "legacy.htm", "<HTML><BODY>upper-case legacy doc</BODY></HTML>")
    got = {(Path(s["path"]).name, s["kind"]) for s in ds.find_screens(str(tmp_path))}
    assert got == {("home.html", "html"), ("legacy.htm", "html")}


def test_find_screens_fragments_plus_stories_yield_stories_only(tmp_path):
    # the DS-repo footgun: preview fragment cards must NOT outrank the real story
    # compositions (and must NOT be reclassified as stories despite living under preview/).
    _mk(tmp_path / "preview" / "guidelines" / "a.card.html", "<div>fragment A</div>")
    _mk(tmp_path / "preview" / "guidelines" / "b.card.html", "<div>fragment B</div>")
    _mk(tmp_path / "stories" / "Home.stories.tsx", "export const Home = () => null")
    screens = ds.find_screens(str(tmp_path))
    assert [s["kind"] for s in screens] == ["story"]
    assert Path(screens[0]["path"]).name == "Home.stories.tsx"
    assert ds.has_usable_designs(str(tmp_path)) is True    # stories make it usable


def test_fragment_only_source_still_usable_via_legacy_html_path(tmp_path):
    # find_screens sees NO screens in a fragments-only dir, but the legacy html path
    # (find_mockups/load_primary_mockup — deliberately untouched) still ingests the
    # fragment, so has_usable_designs stays True (pins test_resolve_clones_git_url).
    _mk(tmp_path / "snippet.html", "<h1>fragment</h1>")
    assert ds.find_screens(str(tmp_path)) == []
    assert ds.has_usable_designs(str(tmp_path)) is True
    assert ds.load_primary_mockup(str(tmp_path))[0] == "snippet.html"


# ── tool-output dirs are never design sources (scan only; manifest wins) ─────

def test_find_screens_skips_tool_output_dirs_but_manifest_wins(tmp_path):
    # Playwright baselines / Storybook builds are artifacts, not designs — the SCAN skips
    # them; a legit png in a sibling screens/ dir is still found.
    _png(tmp_path / "tests" / "__screenshots__" / "baseline.png")
    _mk(tmp_path / "storybook-static" / "index.html",
        "<!doctype html><html><body>storybook build</body></html>")
    _png(tmp_path / "screens" / "home.png")
    screens = ds.find_screens(str(tmp_path))
    assert [(Path(s["path"]).name, s["kind"]) for s in screens] == [("home.png", "image")]

    # find_mockups (legacy) is deliberately NOT extended — byte-identical back-compat:
    # it still sees the storybook-static html in its own scan.
    assert [Path(m).name for m in ds.find_mockups(str(tmp_path))] == ["index.html"]

    # …but the SAME paths listed EXPLICITLY in the manifest ARE included (manifest wins —
    # the author knows what they're pointing at).
    (tmp_path / "design_manifest.md").write_text(
        "- tests/__screenshots__/baseline.png\n- storybook-static/index.html\n",
        encoding="utf-8")
    got = [(Path(s["path"]).name, s["kind"]) for s in ds.find_screens(str(tmp_path))]
    assert got == [("baseline.png", "image"), ("index.html", "html")]


def test_all_tool_output_dir_names_skipped(tmp_path):
    for i, d in enumerate(("__screenshots__", "__snapshots__", "test-results",
                           "playwright-report", "storybook-static", "coverage")):
        _png(tmp_path / d / f"x{i}.png")
    assert ds.find_screens(str(tmp_path)) == []


# ── load_story_excerpts: per-file + total caps ───────────────────────────────

def test_load_story_excerpts_per_and_total_caps(tmp_path):
    st = tmp_path / "stories"
    for name in ("a", "b", "c", "d"):
        _mk(st / f"{name}.stories.tsx", "X" * 5000)
    per = ds.load_story_excerpts(str(tmp_path), per_cap=2000, total_cap=100000)
    assert len(per) == 4 and all(len(text) == 2000 for _n, text in per)   # per-file head cap
    assert [n for n, _t in per] == [                                       # relative names
        "stories/a.stories.tsx", "stories/b.stories.tsx",
        "stories/c.stories.tsx", "stories/d.stories.tsx"]
    tot = ds.load_story_excerpts(str(tmp_path), per_cap=2000, total_cap=5000)
    assert len(tot) == 3                                   # 2000+2000+trimmed-1000 → stop after 3
    assert sum(len(t) for _n, t in tot) == 5000            # STRICT: lands exactly on the cap


def test_load_story_excerpts_trims_boundary_file_to_total_cap(tmp_path):
    # The file that CROSSES total_cap is returned TRIMMED (a head prefix — still the useful
    # imports/composition part), the running total lands ≤ total_cap, and iteration stops.
    st = tmp_path / "stories"
    _mk(st / "a.stories.tsx", "A" * 2000)
    _mk(st / "b.stories.tsx", "B" * 2000)
    boundary = "import { Button } from '@acme/ui'\n" + "C" * 3000
    _mk(st / "c.stories.tsx", boundary)
    _mk(st / "d.stories.tsx", "D" * 2000)                  # after the cap — never returned
    got = ds.load_story_excerpts(str(tmp_path), per_cap=6000, total_cap=5000)
    assert [n for n, _t in got] == ["stories/a.stories.tsx", "stories/b.stories.tsx",
                                    "stories/c.stories.tsx"]
    assert sum(len(t) for _n, t in got) <= 5000            # the invariant, always
    assert sum(len(t) for _n, t in got) == 5000            # boundary file trimmed onto the cap
    trimmed = got[2][1]
    assert len(trimmed) == 1000 and boundary.startswith(trimmed)   # a PREFIX of the file


def test_load_story_excerpts_missing_dir_and_no_stories(tmp_path):
    assert ds.load_story_excerpts(str(tmp_path / "nope")) == []
    _mk(tmp_path / "home.html")                            # html only, no stories
    assert ds.load_story_excerpts(str(tmp_path)) == []


# ── has_usable_designs: ANY kind is usable now ───────────────────────────────

def test_has_usable_designs_stories_only_images_only_and_empty(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ds.has_usable_designs(str(empty)) is False

    stories_only = tmp_path / "stories_only"
    _mk(stories_only / "stories" / "Home.stories.tsx", "export const Home = () => null")
    assert ds.has_usable_designs(str(stories_only)) is True

    images_only = tmp_path / "images_only"
    _png(images_only / "home.png")
    assert ds.has_usable_designs(str(images_only)) is True


# ── find_mockups / load_primary_mockup unchanged (html back-compat) ──────────

def test_find_mockups_still_html_only(tmp_path):
    _mk(tmp_path / "home.html")
    _png(tmp_path / "shot.png")
    _mk(tmp_path / "stories" / "Home.stories.tsx", "export const Home = () => null")
    mocks = [Path(m).name for m in ds.find_mockups(str(tmp_path))]
    assert mocks == ["home.html"]                          # images/stories never returned here


# ── Design agent: imported mode WITHOUT html (stories + images) ──────────────

def test_design_imported_without_html_uses_stories_and_images(llm, ws, tmp_path):
    src = tmp_path / "designrepo"
    _mk(src / "stories" / "Home.stories.tsx",
        "import { Button, Card } from '@acme/ui'\n"
        "export const Home = () => <Card><Button>Go</Button></Card>")
    _png(src / "screens" / "home.png")                     # rendered screen image, NO html
    prd = seed(ws, "proj", "prd", "prd.md", "AC-1 (ui): show the home screen")
    llm.default = ("## Design Context\nImported.\n## Content & Microcopy\n\"Go\"\n"
                   "## Design System\ntokens\n## Flagged Items\nNone.")
    from agents import design
    # react stack known → the kit build runs (so the kit prompt is exercised too).
    out = design.run(base_state(project_id="proj", prd_path=prd, design_source=str(src),
                                detected_stack="Next.js (React + TypeScript)"))

    # (a) still imported mode — NO 3-directions pause, NO A/B/C mockups
    assert "_clarify" not in out
    assert out["design_choice"] == "imported"
    assert not (ws / "proj" / "design" / "mockup_A.html").exists()

    # (b) story excerpts injected as THE AUTHORITATIVE DESIGNED COMPOSITIONS into >1 prompt
    #     (the spec call AND the kit call), with the real composition text present
    texts = llm.user_texts()
    assert texts.count("AUTHORITATIVE DESIGNED COMPOSITIONS") >= 2
    assert "@acme/ui" in texts

    # (c) first image copied as the design_qa baseline + state set; mockup.html still generated
    assert out["design_baseline_png"].endswith("mockup_baseline.png")
    assert Path(out["design_baseline_png"]).exists()
    assert out["design_baseline_pngs"] == [out["design_baseline_png"]]
    assert (ws / "proj" / "design" / "mockup_baseline.png").exists()
    assert out["design_mockup_path"].endswith("mockup.html")
    assert (ws / "proj" / "design" / "mockup.html").exists()
    assert "## Design Source" in (ws / "proj" / "design" / "design_spec.md").read_text()


def test_design_imported_html_still_byte_identical(llm, ws, tmp_path):
    # html present in the source → the ORIGINAL imported-html path: no baseline fields,
    # imported html IS the mockup, stories/images (if any) do not alter it.
    src = tmp_path / "designrepo"
    _mk(src / "home.html",
        "<!doctype html><html><body><h1>Imported Home</h1></body></html>")
    _png(src / "screens" / "home.png")                     # also has an image — must be ignored
    prd = seed(ws, "proj", "prd", "prd.md", "AC-1 (ui): show the home screen")
    llm.default = ("## Design Context\nImported.\n## Content & Microcopy\n\"Go\"\n"
                   "## Design System\ntokens\n## Flagged Items\nNone.")
    from agents import design
    out = design.run(base_state(project_id="proj", prd_path=prd, design_source=str(src),
                                detected_stack="Django + HTMX"))
    assert out["design_choice"] == "imported"
    assert "design_baseline_png" not in out               # html path sets no baseline
    assert "Imported Home" in (ws / "proj" / "design" / "mockup.html").read_text()
    assert not (ws / "proj" / "design" / "mockup_baseline.png").exists()
    assert "AUTHORITATIVE DESIGNED COMPOSITIONS" not in llm.user_texts()  # stories not injected


# ── design_qa: baseline priority + multi-reference ───────────────────────────

def _design_qa_env(ws, monkeypatch):
    from agents import design_qa
    d = ws / "proj" / "design"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mockup.html").write_text("<html>mock</html>")
    shot = ws / "proj" / "tests" / "app_screenshot.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG app")
    rendered = {"called": False}

    def fake_render(mockup_path, out_png):
        rendered["called"] = True
        Path(out_png).parent.mkdir(parents=True, exist_ok=True)
        Path(out_png).write_bytes(b"\x89PNG rendered")
        return True, "ok"
    monkeypatch.setattr(design_qa, "render_mockup_screenshot", fake_render)
    return design_qa, str(d / "mockup.html"), str(shot), rendered


def test_design_qa_prefers_baseline_png_over_rendered_mockup(llm, ws, monkeypatch):
    design_qa, mock, shot, rendered = _design_qa_env(ws, monkeypatch)
    baseline = ws / "proj" / "design" / "mockup_baseline.png"
    baseline.write_bytes(b"\x89PNG baseline")
    llm.default = '{"verdict":"ALIGNED","findings":null}'
    out = design_qa.run(base_state(design_mockup_path=mock, app_screenshot_path=shot,
                                   design_baseline_png=str(baseline)))
    assert out["design_qa_passed"] is True
    assert rendered["called"] is False                     # baseline used → mockup NOT rendered
    ref_paths = [p for _label, p in llm.calls[0]["images"]]
    assert str(baseline) in ref_paths                      # baseline was the reference


def test_design_qa_renders_mockup_when_no_baseline(llm, ws, monkeypatch):
    design_qa, mock, shot, rendered = _design_qa_env(ws, monkeypatch)
    llm.default = '{"verdict":"ALIGNED","findings":null}'
    out = design_qa.run(base_state(design_mockup_path=mock, app_screenshot_path=shot))
    assert out["design_qa_passed"] is True
    assert rendered["called"] is True                      # no baseline → current behavior
    assert len(llm.calls[0]["images"]) == 2                # exactly app + rendered mockup


def test_design_qa_passes_up_to_three_references(llm, ws, monkeypatch):
    design_qa, mock, shot, _rendered = _design_qa_env(ws, monkeypatch)
    d = ws / "proj" / "design"
    for n in (1, 2, 3, 4):
        (d / ("mockup_baseline.png" if n == 1 else f"mockup_baseline_{n}.png")).write_bytes(
            f"\x89PNG{n}".encode())
    b = [str(d / "mockup_baseline.png")] + [str(d / f"mockup_baseline_{n}.png") for n in (2, 3, 4)]
    llm.default = '{"verdict":"ALIGNED","findings":null}'
    design_qa.run(base_state(design_mockup_path=mock, app_screenshot_path=shot,
                             design_baseline_png=b[0], design_baseline_pngs=b))
    imgs = [p for _label, p in llm.calls[0]["images"]]
    assert len(imgs) == 4                                   # 1 app + 3 references (cap)
    assert b[3] not in imgs and b[1] in imgs and b[2] in imgs


# ── absent design source → new fields absent, behavior unchanged ─────────────

def test_absent_design_source_sets_no_baseline_fields(llm, ws):
    from agents import design
    prd = seed(ws, "proj", "prd", "prd.md", "AC-1 (ui): show home")
    choice = {"from": "design", "to": "ceo",
              "question": "DESIGN DIRECTION CHOICE (CEO/CTO — human pick):", "answer": "A"}
    llm.default = ("## Design Context\nx\n## Content & Microcopy\ny\n## Design System\ntokens\n"
                   "## Flagged Items\nNone.")
    out = design.run(base_state(project_id="proj", prd_path=prd, detected_stack="Python",
                                qa_log=[choice]))
    assert out["design_choice"] == "A"
    assert "design_baseline_png" not in out and "design_baseline_pngs" not in out
    assert "AUTHORITATIVE DESIGNED COMPOSITIONS" not in llm.user_texts()


def test_resolve_design_source_none_without_any_design(tmp_path):
    from agents.design import _resolve_design_source
    assert _resolve_design_source(base_state()) is None                       # unset
    assert _resolve_design_source(base_state(design_source="")) is None        # empty
    assert _resolve_design_source(base_state(design_source=str(tmp_path))) is None  # empty dir
