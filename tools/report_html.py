"""
HTML review layer (CEO decision 2026-06-12, after thariqs.github.io/html-effectiveness).

DESIGN DECISION — dual-surface, not migration: `.md` stays the canonical agent-readable
artifact (token-cheap, regex-parseable, git-diffable); THIS module renders the
human-facing HTML pages for the moments a HUMAN decides:
  - review/design_options.html — the 3 design directions, side by side, for the choice
  - review/prd_gate.html      — the PRD approval
  - review/pr_gate.html       — the ship decision (integration badges, screenshots,
                                QA report, security findings)
  - .agent/ledger.html        — the project's feature history as a browsable page

Everything here is DETERMINISTIC templating — zero LLM calls, zero marginal tokens.
Rendering must never break a gate: every public function catches and returns None.
"""

import html as _html
import re
from pathlib import Path

from tools.file_io import WORKSPACE_ROOT, read_artifact

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     margin:0;background:#f6f7f9;color:#1a1d21;line-height:1.55}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px}
h1{font-size:1.5rem;margin:.2em 0 .6em}
h2{font-size:1.15rem;margin:1.4em 0 .5em;border-bottom:1px solid #e2e5e9;padding-bottom:.25em}
.card{background:#fff;border:1px solid #e2e5e9;border-radius:10px;padding:18px 20px;margin:14px 0;
      box-shadow:0 1px 2px rgba(0,0,0,.04)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.78rem;font-weight:600;
       margin-right:6px}
.ok{background:#e6f6ec;color:#176939}.fail{background:#fdebec;color:#a52833}
.warn{background:#fef3e2;color:#92560a}.info{background:#e8f0fe;color:#1a4fa0}
.grid{display:grid;gap:16px}.cols2{grid-template-columns:1fr 1fr}.cols3{grid-template-columns:1fr 1fr 1fr}
@media(max-width:900px){.cols2,.cols3{grid-template-columns:1fr}}
iframe{width:100%;height:560px;border:1px solid #d6dade;border-radius:8px;background:#fff}
img.shot{width:100%;border:1px solid #d6dade;border-radius:8px}
pre{background:#f0f2f5;border-radius:8px;padding:12px;overflow-x:auto;font-size:.85rem}
code{background:#f0f2f5;border-radius:4px;padding:1px 5px;font-size:.9em}
details summary{cursor:pointer;font-weight:600;margin:.4em 0}
.md ul{padding-left:1.3em}.md li{margin:.15em 0}
.answer{background:#1a1d21;color:#e8eaed;border-radius:10px;padding:14px 18px;font-size:.95rem}
.answer code{background:#33373d;color:#ffd866}
.muted{color:#6b7280;font-size:.88rem}
.kpis{display:flex;flex-wrap:wrap;gap:22px}
.kpi{min-width:96px}.kpi .n{font-size:1.5rem;font-weight:700}.kpi .l{color:#6b7280;font-size:.78rem}
.barrow{display:flex;align-items:center;gap:10px;margin:5px 0}
.barlab{width:150px;font-size:.83rem;text-align:right;color:#374151;flex:none;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.barwrap{flex:1;background:#eef0f3;border-radius:6px;overflow:hidden;height:18px;min-width:60px}
.bar{display:block;height:100%}
.barval{width:172px;font-size:.78rem;color:#6b7280;flex:none}
.flow{display:flex;flex-wrap:wrap;align-items:center;gap:3px;line-height:2.1}
.chip{display:inline-block;padding:3px 9px;border-radius:6px;color:#fff;font-size:.78rem;font-weight:600}
.arrow{color:#9aa1aa;font-weight:700}
.lk a{display:inline-block;margin:2px 14px 2px 0;font-size:.9rem}
"""


def _page(title: str, body: str) -> str:
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><div class=\"wrap\"><h1>{_html.escape(title)}</h1>{body}</div></body></html>")


def _md(md_text: str) -> str:
    """Tiny markdown→HTML for human reading (headings, lists, bold, code). Anything
    fancier stays a <pre>. Deliberately dependency-free."""
    out, in_ul, in_code = [], False, False
    for line in (md_text or "").splitlines():
        if line.strip().startswith("```"):
            out.append("<pre>" if not in_code else "</pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(_html.escape(line))
            continue
        esc = _html.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        m = re.match(r"^(#{1,4})\s+(.*)", esc)
        if m:
            if in_ul:
                out.append("</ul>"); in_ul = False
            lvl = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
            continue
        if re.match(r"^\s*[-*]\s+", esc):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            item = re.sub(r"^\s*[-*]\s+", "", esc)
            out.append(f"<li>{item}</li>")
            continue
        if in_ul:
            out.append("</ul>"); in_ul = False
        out.append(f"<p>{esc}</p>" if esc.strip() else "")
    if in_ul:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")
    return f'<div class="md">{chr(10).join(out)}</div>'


def _review_dir(project_id: str) -> Path:
    d = WORKSPACE_ROOT / project_id / "review"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mockup_iframe(project_id: str, mockup_file: str) -> str:
    """Embed the mockup INLINE via srcdoc, NOT src="path": a file:// page cannot load a
    file:// iframe (the browser gives the child document an opaque origin and renders it
    blank), so the side-by-side board was empty whenever the report was opened from disk.
    srcdoc renders the mockup as a same-origin inline document — works from file:// AND
    when served over http. Falls back to the path reference if the mockup isn't on disk."""
    try:
        html = (WORKSPACE_ROOT / project_id / "design" / mockup_file).read_text(
            encoding="utf-8", errors="replace")
        return f'<iframe srcdoc="{_html.escape(html, quote=True)}" loading="lazy"></iframe>'
    except OSError:
        return (f'<iframe src="../design/{_html.escape(mockup_file)}" '
                f'loading="lazy"></iframe>')


def render_design_options(project_id: str, options: list) -> str | None:
    """The 3 design directions side by side — mockup iframes + the rationale for each.
    options: [{"id","title","rationale","mockup_file"}] with mockup_file relative to
    the run's design/ dir."""
    try:
        cards = []
        for o in options:
            cards.append(
                f"<div class=\"card\"><h2>Direction {_html.escape(o['id'])} — "
                f"{_html.escape(o.get('title') or '')}</h2>"
                f"<p>{_html.escape(o.get('rationale') or '')}</p>"
                + _mockup_iframe(project_id, o['mockup_file'])
                + "</div>")
        body = (
            "<div class=\"answer\">Pick the direction this feature should ship with: "
            "reply <code>A</code>, <code>B</code> or <code>C</code> to the pipeline's "
            "question (optionally add tweaks, e.g. <code>B, but use the lighter "
            "header</code>).</div>"
            + "".join(cards))
        path = _review_dir(project_id) / "design_options.html"
        path.write_text(_page("Design directions — pick one", body), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def _integration_badges(report: str) -> str:
    rows = []
    for name, verdict in re.findall(r"(?:===?|---) ?([\w][\w ()/\-*.]*?) — (OK|FAILED) (?:===|---)",
                                    report or ""):
        cls = "ok" if verdict == "OK" else "fail"
        rows.append(f"<span class=\"badge {cls}\">{_html.escape(name.strip())}: {verdict}</span>")
    return " ".join(rows) or "<span class=\"badge info\">no integration report</span>"


def render_gate(state: dict, stage: str) -> str | None:
    """The human gate dashboard: prd (approve the PRD) or pr (the ship decision)."""
    try:
        pid = state["project_id"]
        run_dir = WORKSPACE_ROOT / pid
        parts = []
        if stage == "prd":
            parts.append("<div class=\"answer\">Approve this PRD to start the build, or "
                         "reject with feedback (loops back to the PM).</div>")
            parts.append(f"<div class=\"card\">{_md(read_artifact(str(run_dir / 'prd' / 'prd.md')))}</div>")
        else:
            integ = read_artifact(str(run_dir / "tests" / "integration_report.md"))
            parts.append("<div class=\"answer\">Approve to OPEN THE PR, or reject with "
                         "feedback (loops back to the engineer).</div>")
            parts.append(f"<div class=\"card\"><h2>Integration</h2>{_integration_badges(integ)}"
                         + (f"<details><summary>full report</summary><pre>"
                            f"{_html.escape(integ[-6000:])}</pre></details>" if integ else "")
                         + "</div>")
            shots = []
            for label, fname in (("Live app", "app_screenshot.png"),
                                 ("Design mockup", "mockup_screenshot.png")):
                if (run_dir / "tests" / fname).exists():
                    shots.append(f"<div><p class=\"muted\">{label}</p>"
                                 f"<img class=\"shot\" src=\"../tests/{fname}\"></div>")
            if shots:
                parts.append(f"<div class=\"card\"><h2>Does it look right?</h2>"
                             f"<div class=\"grid cols2\">{''.join(shots)}</div></div>")
            qa = read_artifact(str(run_dir / "tests" / "qa_report.md"))
            if qa:
                parts.append(f"<div class=\"card\"><h2>QA sign-off</h2>{_md(qa)}</div>")
            sec = state.get("security_warnings") or []
            if sec:
                items = "".join(f"<li><span class=\"badge warn\">!</span> {_html.escape(s)}</li>"
                                for s in sec)
                parts.append(f"<div class=\"card\"><h2>Security findings</h2><ul>{items}</ul></div>")
            cq = state.get("code_quality") or []
            if cq:
                items = "".join(f"<li><span class=\"badge warn\">!</span> {_html.escape(s)}</li>"
                                for s in cq)
                parts.append(f"<div class=\"card\"><h2>Code quality (advisory)</h2><ul>{items}</ul></div>")
        path = _review_dir(pid) / f"{stage}_gate.html"
        title = "PRD approval" if stage == "prd" else "Ship decision (PR gate)"
        path.write_text(_page(title, "".join(parts)), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def render_ledger(project_root: str) -> str | None:
    """The project's feature history as a browsable page (.agent/ledger.html)."""
    try:
        md_path = Path(project_root) / ".agent" / "ledger.md"
        if not md_path.exists():
            return None
        out = Path(project_root) / ".agent" / "ledger.html"
        out.write_text(_page("Project feature ledger",
                             f"<div class=\"card\">{_md(md_path.read_text(encoding='utf-8'))}</div>"),
                       encoding="utf-8")
        return str(out)
    except Exception:
        return None


# ── Human audit folder (every discussion, conclusion, and WHY) ───────────────

# fb_kind → (from-actor, to-actor, what-happened, why-this-conclusion-template).
# Every feedback event in the run trace is a real moment where an actor's work was
# judged; the audit turns each into a plain-English "who/what/why" the human can read.
_FB = {
    "prd_gate_reject":    ("CEO/CTO (PRD gate)", "PM", "PRD rejected — changes requested"),
    "pr_gate_reject":     ("CEO/CTO (PR gate)", "Engineer", "Ship rejected — changes requested"),
    "critic_design_retry": ("Design Critic", "Design", "Design sent back to fix gaps"),
    "critic_architect_retry": ("Architect Critic", "Architect", "Tech spec sent back to fix gaps"),
    "design_qa_misaligned": ("Design QA (vision)", "Design / Engineer",
                             "Live app did NOT match the chosen mockup"),
    "e2e_lint_drop":      ("QA (self-lint)", "QA", "An e2e spec was DROPPED — failed the deterministic lint"),
    "e2e_revision_needed": ("Integration", "QA", "e2e failed on a healthy app — specs revised"),
    "guard_violation":    ("Codegen guard", "Engineer", "A protected-path write was BLOCKED"),
    "kit_domain_violation": ("Codegen guard", "Design", "An out-of-kit write was BLOCKED"),
    "interface_regression": ("Interface-Contract freeze", "Design",
                             "A prior-phase testid/microcopy was DROPPED — restore forced"),
}


def _classify_fb(e: dict) -> tuple:
    kind = e.get("fb_kind", "")
    if kind in _FB:
        frm, to, what = _FB[kind]
    elif kind.startswith("integration_"):
        stage = kind.split("_", 1)[1]
        frm, to = "Integration", ("QA" if stage in ("e2e", "coverage") else "Engineer")
        what = f"Integration FAILED at the {stage} stage"
    else:
        frm, to, what = "System", e.get("agent", "?"), kind.replace("_", " ")
    return frm, to, what


def render_audit(state: dict, trace_path: str = "", retro: dict = None,
                 overseer: dict = None) -> str | None:
    """The human audit folder: a single browsable page showing the run's actors, every
    discussion (who/what/feedback/conclusion/why), the AC coverage, links to every
    rendered artifact, and what the team learned. Deterministic; never raises."""
    try:
        from evals import run_stats
        pid = state["project_id"]
        run_dir = WORKSPACE_ROOT / pid
        adir = run_dir / "review" / "audit"
        (adir / "artifacts").mkdir(parents=True, exist_ok=True)
        events = run_stats.load_events(trace_path) if trace_path else []

        parts = [_audit_header(state, events, overseer)]
        parts.append(_audit_discussions(state, events))
        parts.append(_audit_coverage(state))
        parts.append(_audit_artifacts(state, run_dir, adir))
        parts.append(_audit_retro(retro))
        path = adir / "index.html"
        path.write_text(_page(f"Run audit — {state.get('feature_request','')[:80]}",
                              "".join(p for p in parts if p)), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def _audit_header(state, events, overseer) -> str:
    decided = []
    if state.get("design_choice"):
        opt = next((o for o in (state.get("design_options") or [])
                    if o["id"] == state["design_choice"]), None)
        title = opt["title"] if opt else ""
        decided.append(f"Design direction chosen by the human: <b>{_html.escape(state['design_choice'])} "
                       f"— {_html.escape(title)}</b>")
    decided.append("PRD approved" if state.get("prd_approved") else "PRD not approved")
    decided.append("PR approved (shipped)" if state.get("pr_approved") else "PR not approved")
    llm = [e for e in events if e.get("kind") == "llm_call"]
    nodes = [e for e in events if e.get("kind") == "node_exec"]
    ov = ""
    if overseer:
        verdict = "TRUSTWORTHY" if overseer.get("ok") else "NEEDS HUMAN REVIEW"
        cls = "ok" if overseer.get("ok") else "fail"
        ov = f'<span class="badge {cls}">{verdict}</span>'
    metrics = (f"{len(nodes)} node executions · {len(llm)} LLM calls · "
               f"{sum(e.get('out_tokens',0) for e in llm)} output tokens")
    return ("<div class=\"answer\">This page is an AUDIT of one feature run — every "
            "actor, discussion, decision, and the reasoning behind it.</div>"
            f"<div class=\"card\"><h2>Run summary {ov}</h2>"
            f"<p><b>Feature:</b> {_html.escape(state.get('feature_request','') or '')}</p>"
            f"<p><b>Outcome:</b> tests {'PASS' if state.get('tests_passed') else 'not green'} · "
            + " · ".join(decided) + f"</p><p class=\"muted\">{metrics}</p></div>")


def _audit_discussions(state, events) -> str:
    rows = []
    # 1) Agent↔agent consults and agent↔CEO Q&A (the qa_log carries the full exchange)
    for e in (state.get("qa_log") or []):
        frm, to = e.get("from", "?"), e.get("to", "?")
        q, a = e.get("question") or "", e.get("answer")
        actor = f"{_html.escape(frm)} → {_html.escape(to)}"
        concl = (_html.escape(a) if a else "<i>awaiting answer</i>")
        rows.append(("info", actor, "Clarification / decision", _html.escape(q), concl,
                     "The asker needed input it could not resolve alone; this answer unblocked it."))
    # 2) Feedback moments from the trace (gates, critics, integration, guards, vision QA)
    for e in events:
        if e.get("kind") != "feedback":
            continue
        frm, to, what = _classify_fb(e)
        why = (e.get("text") or "").strip()[:700]
        rows.append(("warn", f"{_html.escape(frm)} → {_html.escape(to)}", what,
                     "", "Sent back for rework — see reason.", _html.escape(why)))
    if not rows:
        return ""
    cards = []
    for cls, actor, topic, what_about, conclusion, why in rows:
        about = f"<p class=\"muted\">{what_about}</p>" if what_about else ""
        cards.append(
            f"<div class=\"card\"><span class=\"badge {cls}\">{actor}</span> "
            f"<b>{_html.escape(topic)}</b>{about}"
            f"<p><b>Conclusion:</b> {conclusion}</p>"
            f"<details><summary>why / detail</summary><pre>{why}</pre></details></div>")
    return "<h2>Discussions &amp; decisions — who, what, and why</h2>" + "".join(cards)


def _audit_coverage(state) -> str:
    try:
        from tools import contract
        from tools.file_io import read_artifact, code_root
        prd = read_artifact(state["prd_path"]) if state.get("prd_path") else ""
        acs = contract.parse_acs(prd)
        if not acs:
            return ""
        root = code_root(state)
        unit, e2e = [], []
        for sub in ("tests", "backend/tests", "frontend/tests"):
            d = root / sub
            if d.is_dir():
                unit += [p.read_text(encoding="utf-8", errors="replace") for p in d.rglob("test_*.py")]
        for rel in (state.get("e2e_files") or []):
            p = root / rel
            if p.exists():
                e2e.append(p.read_text(encoding="utf-8", errors="replace"))
        cov = contract.coverage(acs, unit, e2e)
        rows = ""
        for ac_id, c in cov["map"].items():
            u = "✓" if c["unit"] else "·"
            ev = "✓" if c["e2e"] else "·"
            gap = "" if (c["unit"] or c["e2e"]) and not (c["surface"] == "ui" and not c["e2e"]) \
                else " style=\"background:#fdebec\""
            rows += (f"<tr{gap}><td>{ac_id}</td><td>{c['surface']}</td>"
                     f"<td>{_html.escape(c['text'][:90])}</td><td>{u}</td><td>{ev}</td></tr>")
        return ("<h2>Acceptance-criteria coverage (the feature contract)</h2><div class=\"card\">"
                "<table style=\"width:100%;border-collapse:collapse\"><tr><th>AC</th><th>surface</th>"
                "<th>criterion</th><th>unit</th><th>e2e</th></tr>" + rows + "</table></div>")
    except Exception:
        return ""


def _audit_artifacts(state, run_dir, adir) -> str:
    links = []
    md_arts = [("PRD", "prd/prd.md"), ("Design spec", "design/design_spec.md"),
               ("Interface contract (manifest)", "design/components_manifest.md"),
               ("Tech spec", "design/tech_spec.md"), ("QA sign-off", "tests/qa_report.md"),
               ("Integration report", "tests/integration_report.md"),
               ("Design-QA verdict", "tests/design_qa.md"), ("Run statistics", "stats.md")]
    for label, rel in md_arts:
        src = run_dir / rel
        if src.exists():
            html = adir / "artifacts" / (rel.replace("/", "__") + ".html")
            html.write_text(_page(label, f"<div class=\"card\">{_md(src.read_text(encoding='utf-8'))}</div>"),
                            encoding="utf-8")
            links.append(f'<li><a href="artifacts/{html.name}">{_html.escape(label)}</a></li>')
    # direct-link the visual artifacts (already HTML / images)
    for label, rel in (("Design options (3 directions)", "review/design_options.html"),
                       ("Chosen mockup", "design/mockup.html"),
                       ("Live-app screenshot", "tests/app_screenshot.png"),
                       ("Mockup screenshot", "tests/mockup_screenshot.png")):
        if (run_dir / rel).exists():
            links.append(f'<li><a href="../../../{rel}">{_html.escape(label)}</a></li>')
    return ("<h2>Artifacts</h2><div class=\"card\"><ul>" + "".join(links) + "</ul></div>") if links else ""


def _audit_retro(retro) -> str:
    if not retro:
        return ""
    items = "".join(f"<li><b>{_html.escape(a)}</b>: {_html.escape(l)}</li>"
                    for a, ls in retro.items() for l in ls)
    return ("<h2>What the team learned this run (applied next run)</h2>"
            f"<div class=\"card\"><ul>{items}</ul></div>") if items else ""


# ── Per-run flight recorder (quantitative observability) ─────────────────────
# The audit (above) answers "who decided what and why"; this answers "what did the run
# DO, and where did the time/tokens go" — the visual layer the text-dump stats.html
# lacked. Built ENTIRELY from the trace via run_stats.aggregate (deterministic, zero LLM).

# node → colour, grouped by phase so loops are visible at a glance.
_NODE_GROUP = [
    (("ceo", "ceo_qa", "prd_gate", "pr_gate"), "#1a1d21"),     # human / gate
    (("triage", "surveyor"), "#6b7280"),                       # intake
    (("pm",), "#1a4fa0"),                                       # product
    (("design", "critic_design", "design_qa"), "#7a3cc0"),     # design
    (("architect", "critic_architect"), "#0f766e"),            # architecture
    (("test_author",), "#b45309"),                             # tests
    (("engineer",), "#176939"),                                # build
    (("qa", "integration"), "#c2410c"),                        # verify
    (("ship", "devops"), "#0e7490"),                           # ship / ops
]


def _node_color(node: str) -> str:
    for names, col in _NODE_GROUP:
        if node in names:
            return col
    return "#475569"


def _bar(label: str, value: float, maxval: float, color: str, valtext: str) -> str:
    pct = 0 if maxval <= 0 else max(2, round(100 * value / maxval))
    return (f'<div class="barrow"><span class="barlab">{_html.escape(label)}</span>'
            f'<span class="barwrap"><span class="bar" style="width:{pct}%;background:{color}">'
            f'</span></span><span class="barval">{_html.escape(valtext)}</span></div>')


def _path_chips(events: list) -> str:
    """The ACTUAL node path, consecutive repeats collapsed to `node ×N`. A node
    re-appearing later in the sequence is a loop (e.g. an engineer⇄QA fix round)."""
    seq = [e.get("node") for e in events if e.get("kind") == "node_exec" and e.get("node")]
    if not seq:   # older traces carry only `node` events (no node_exec wall timing)
        seq = [e.get("node") for e in events if e.get("kind") == "node" and e.get("node")]
    if not seq:
        return ""
    collapsed = []
    for n in seq:
        if collapsed and collapsed[-1][0] == n:
            collapsed[-1][1] += 1
        else:
            collapsed.append([n, 1])
    chips = [f'<span class="chip" style="background:{_node_color(n)}">'
             f'{_html.escape(n + ("" if c == 1 else f" ×{c}"))}</span>' for n, c in collapsed]
    return '<div class="flow">' + '<span class="arrow">→</span>'.join(chips) + '</div>'


def render_run(state: dict, trace_path: str = "", overseer: dict = None,
               autonomy: dict = None) -> str | None:
    """The per-run FLIGHT RECORDER (review/run.html): a visual, deterministic dashboard of
    what the run did — the autonomy metric (human interventions), the node path (with
    loops), where wall-time went, model spend by tier and by node, and a loop/rework
    summary — all from the trace. Cross-links the audit + gate pages. Zero LLM; never
    raises (returns None on any failure)."""
    try:
        from collections import Counter
        from evals import run_stats
        pid = state["project_id"]
        events = run_stats.load_events(trace_path) if trace_path else []
        agg = (run_stats.aggregate(trace_path) if trace_path
               else {"totals": {}, "nodes": {}, "by_tier": {}, "timeline": []})
        t, fmt = agg["totals"], run_stats._fmt_ms
        auton = autonomy or run_stats.compute_autonomy(events, state)

        ov = ""
        if overseer:
            cls = "ok" if overseer.get("ok") else "fail"
            ov = (f'<span class="badge {cls}">'
                  f'{"TRUSTWORTHY" if overseer.get("ok") else "NEEDS HUMAN REVIEW"}</span>')
        outcome = ("tests " + ("PASS" if state.get("tests_passed") else "not green")
                   + " · PR " + ("approved" if state.get("pr_approved") else "not approved"))
        kpis = "".join(
            f'<div class="kpi"><div class="n">{n}</div><div class="l">{lbl}</div></div>'
            for n, lbl in [
                (fmt(t.get("elapsed_ms", 0)), "elapsed"),
                (fmt(t.get("wall_ms", 0)), "compute"),
                (fmt(t.get("human_wait_ms", 0)), "human wait"),
                (t.get("llm_calls", 0), "LLM calls"),
                (f'{t.get("in_tokens",0)//1000}k/{t.get("out_tokens",0)//1000}k', "tok in/out"),
                (fmt(t.get("llm_ms", 0)), "model time")])
        head = (f'<div class="card"><h2>Run summary {ov}</h2>'
                f'<p><b>Feature:</b> {_html.escape((state.get("feature_request") or "")[:140])}</p>'
                f'<p class="muted">{outcome}</p><div class="kpis">{kpis}</div></div>')

        # Autonomy (§3.3 / I10): the headline number a software company manages — how
        # often the humans had to act vs just rubber-stamp the mandatory gates.
        auton_kpis = "".join(
            f'<div class="kpi"><div class="n">{n}</div><div class="l">{lbl}</div></div>'
            for n, lbl in [
                (f'{round(auton["autonomy_rate"] * 100)}%', "autonomy rate"),
                (auton["interventions"], "human interventions"),
                (auton["approvals"], "gate approvals"),
                (auton["agent_steps"], "agent steps")])
        auton_card = (
            f'<div class="card"><h2>Autonomy</h2><div class="kpis">{auton_kpis}</div>'
            f'<p class="muted">Interventions = {auton["clarifications"]} CEO clarification(s) + '
            f'{auton["rejections"]} gate reject(s) + {auton["manual_edits"]} manual edit(s). '
            f'Rate = approvals ÷ (approvals + interventions): 100% means the human only '
            f'rubber-stamped the mandatory gates. Drive interventions to zero.</p></div>')

        path = _path_chips(events)
        path_card = (f'<div class="card"><h2>Path taken</h2>{path}'
                     '<p class="muted">Each chip is a node execution; ×N = consecutive '
                     'repeats. A node re-appearing later is a loop (engineer⇄QA fix rounds, '
                     'critic retries, integration bounces).</p></div>') if path else ""

        named = {k: v for k, v in agg["nodes"].items() if k != "(unattributed)"}
        time_card = ""
        if any(v["wall_ms"] for v in named.values()):
            ranked = sorted(named.items(), key=lambda kv: -kv[1]["wall_ms"])[:9]
            mx = ranked[0][1]["wall_ms"] or 1
            time_card = '<div class="card"><h2>Where time went (by node)</h2>' + "".join(
                _bar(k, v["wall_ms"], mx, _node_color(k),
                     f'{fmt(v["wall_ms"])} · {v["visits"]}×'
                     + (f' · {v["files_written"]} files' if v["files_written"] else ""))
                for k, v in ranked) + "</div>"

        tiers = agg["by_tier"]
        tier_card = ""
        if tiers:
            mx = max((d["in_tokens"] + d["out_tokens"]) for d in tiers.values()) or 1
            order = [x for x in ("reason", "strong", "fast") if x in tiers] + \
                    [x for x in tiers if x not in ("reason", "strong", "fast")]
            tcol = {"fast": "#1a4fa0", "strong": "#176939", "reason": "#7a3cc0"}
            tier_card = '<div class="card"><h2>Model spend by tier (tokens)</h2>' + "".join(
                _bar(tier, tiers[tier]["in_tokens"] + tiers[tier]["out_tokens"], mx,
                     tcol.get(tier, "#475569"),
                     f'{tiers[tier]["calls"]} calls · {tiers[tier]["in_tokens"]//1000}k in / '
                     f'{tiers[tier]["out_tokens"]//1000}k out') for tier in order) + (
                '<p class="muted">Cost proxy — on the claude-cli backend this is plan quota, '
                'not $. strong/reason = Opus, fast = Haiku.</p></div>')

        fb = [e for e in events if e.get("kind") == "feedback"]
        dec_card = ""
        if fb:
            counts = Counter(_classify_fb(e)[2] for e in fb)
            items = "".join(f'<li><span class="badge warn">{c}×</span> {_html.escape(w)}</li>'
                            for w, c in counts.most_common())
            dec_card = (f'<div class="card"><h2>Loops &amp; rework ({len(fb)})</h2><ul>{items}</ul>'
                        '<p class="muted">Every send-back that cost a cycle. Full who/what/why '
                        'is in the audit page below.</p></div>')

        link_specs = [("Ship decision (PR gate)", "review/pr_gate.html", "pr_gate.html"),
                      ("PRD approval", "review/prd_gate.html", "prd_gate.html"),
                      ("Full audit (who / what / why)", "review/audit/index.html", "audit/index.html"),
                      ("Design directions", "review/design_options.html", "design_options.html"),
                      ("Run statistics (raw)", "stats.md", "../stats.md")]
        links = "".join(f'<a href="{href}">{_html.escape(label)}</a>'
                        for label, rel, href in link_specs
                        if (WORKSPACE_ROOT / pid / rel).exists())
        link_card = f'<div class="card lk"><h2>Related</h2>{links}</div>' if links else ""

        body = head + auton_card + path_card + time_card + tier_card + dec_card + link_card
        out = _review_dir(pid) / "run.html"
        out.write_text(_page(f"Run flight recorder — {(state.get('feature_request') or '')[:60]}", body),
                       encoding="utf-8")
        return str(out)
    except Exception:
        return None
