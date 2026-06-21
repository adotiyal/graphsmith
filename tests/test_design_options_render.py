"""The design-directions board (review/design_options.html) must embed the mockups
INLINE via srcdoc, not reference them with file:// iframe srcs — a file:// page gives a
file:// iframe an opaque origin and renders it blank, so the visual board was empty
whenever a human opened the report from disk (live-reported on the phase-2 run)."""
from tools import report_html


def test_design_options_embeds_mockup_via_srcdoc(tmp_path, monkeypatch):
    monkeypatch.setattr(report_html, "WORKSPACE_ROOT", tmp_path)
    pid = "proj-x"
    design_dir = tmp_path / pid / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "mockup_A.html").write_text(
        "<!DOCTYPE html><html><body><h1>MOCKUP_MARKER</h1></body></html>", encoding="utf-8")

    options = [{"id": "A", "title": "Grid", "rationale": "why", "mockup_file": "mockup_A.html"}]
    out = open(report_html.render_design_options(pid, options)).read()

    assert "srcdoc=" in out                          # inline, renders under file://
    assert '<iframe src="../design/' not in out      # no blank file:// iframe
    assert "MOCKUP_MARKER" in out                    # the real mockup HTML is embedded


def test_design_options_falls_back_to_src_when_mockup_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(report_html, "WORKSPACE_ROOT", tmp_path)
    pid = "proj-y"
    (tmp_path / pid / "review").mkdir(parents=True)   # review dir exists; no design/mockup

    options = [{"id": "A", "title": "Grid", "rationale": "why", "mockup_file": "mockup_A.html"}]
    out = open(report_html.render_design_options(pid, options)).read()

    assert 'src="../design/mockup_A.html"' in out     # graceful fallback, never crashes
