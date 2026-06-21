"""design._extract_html must return the COMPLETE mockup document, never a fragment. The
old non-greedy ```...``` match grabbed a tiny MIDDLE slice when the model emitted a stray
code fence (e.g. a snippet in the SEO section), so 2 of 3 phase-3 mockups shipped as 4-6KB
fragments and the side-by-side board rendered blank/garbled."""
from agents.design import _extract_html

FULL = ('<!DOCTYPE html><html lang="en"><head><title>Board</title>'
        '<script src="https://cdn.tailwindcss.com"></script></head>'
        '<body><main>REAL BOARD CONTENT</main></body></html>')


def test_full_doc_recovered_despite_a_stray_code_fence():
    # the live bug: a fenced snippet sitting BEFORE the real document
    out = _extract_html("Here is an example toast:\n```html\n<p>toast snippet</p>\n```\n" + FULL)
    assert out == FULL                                   # the whole board, not the 1-line snippet
    assert "REAL BOARD CONTENT" in out and "toast snippet" not in out


def test_clean_doc_passthrough():
    assert _extract_html(FULL) == FULL


def test_strips_preamble_before_doctype():
    assert _extract_html("Sure! Here's the mockup:\n\n" + FULL) == FULL


def test_whole_doc_wrapped_in_one_fence():
    assert _extract_html("```html\n" + FULL + "\n```") == FULL


def test_truncated_doc_returns_best_effort_not_a_fragment():
    out = _extract_html(FULL.replace("</body></html>", "<!-- cut off"))
    assert out.lower().startswith("<!doctype html") and "REAL BOARD CONTENT" in out
