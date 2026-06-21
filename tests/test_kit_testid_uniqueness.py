"""
Kit testid-uniqueness check — catches the responsive dual-layout duplicate-testid hazard
(a shared component rendered in BOTH the desktop and mobile layouts emits the same
data-testid twice → Playwright strict-mode failure that the engineer can't fix because the
kit is design-owned). Surfaced live in MadClub phase 1; this hardens the pipeline against it.

The detector is precise: a component differentiated per usage (e.g. scope="-card") has
distinct usage strings and is NOT flagged — only identical repeated usages are.
"""

from tools.registry import check_kit_testid_uniqueness, _duplicate_testid_components

_DUP = """
function RowActions({ a }) {
  return <button data-testid={`row-actions-${a.id}`}>x</button>;
}
export function List({ items }) {
  return (<>
    <table>{items.map((a) => (<tr key={a.id}><RowActions a={a} /></tr>))}</table>
    <ul className="md:hidden">{items.map((a) => (<li key={a.id}><RowActions a={a} /></li>))}</ul>
  </>);
}
"""

_SCOPED = _DUP.replace("<RowActions a={a} /></li>", '<RowActions a={a} scope="-card" /></li>')

_SINGLE = """
function Badge({ d }) { return <span data-testid={`difficulty-badge-${d}`}>{d}</span>; }
export function Row({ a }) { return <td><Badge d={a.difficulty} /></td>; }
"""

_NO_TESTID = """
function Spacer() { return <div className="h-4" />; }
export function Wrap() { return (<><Spacer /><Spacer /></>); }
"""


# ── pure detector ─────────────────────────────────────────────────────────────

def test_flags_identical_repeated_usage_of_a_testid_component():
    assert _duplicate_testid_components(_DUP) == ["RowActions"]


def test_scoped_per_layout_usage_is_not_flagged():
    assert _duplicate_testid_components(_SCOPED) == []      # scope="-card" differs → unique


def test_single_use_component_is_not_flagged():
    assert _duplicate_testid_components(_SINGLE) == []


def test_component_without_testid_used_twice_is_not_flagged():
    assert _duplicate_testid_components(_NO_TESTID) == []


# ── directory-level check ───────────────────────────────────────────────────

def test_check_flags_dup_in_a_kit_dir(tmp_path):
    (tmp_path / "AdventuresList.tsx").write_text(_DUP)
    findings = check_kit_testid_uniqueness(tmp_path)
    assert findings and any("RowActions" in f and "strict-mode" in f for f in findings)


def test_check_clean_on_scoped_kit(tmp_path):
    (tmp_path / "AdventuresList.tsx").write_text(_SCOPED)
    assert check_kit_testid_uniqueness(tmp_path) == []


def test_check_flags_literal_testid_across_files(tmp_path):
    (tmp_path / "A.tsx").write_text('export const A = () => <div data-testid="adventure-sheet" />;')
    (tmp_path / "B.tsx").write_text('export const B = () => <div data-testid="adventure-sheet" />;')
    findings = check_kit_testid_uniqueness(tmp_path)
    assert any("adventure-sheet" in f for f in findings)


def test_check_never_raises_on_missing_dir(tmp_path):
    assert check_kit_testid_uniqueness(tmp_path / "nope") == []
