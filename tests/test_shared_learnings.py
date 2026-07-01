"""
Committed shared-learnings tier + the promote CLI (tools/learnings.py).

The shared tier (learnings/shared/<agent>.md, committed/un-ignored) ships GENERIC,
human-promoted lessons WITH the harness; the local store stays gitignored + per-install.
`augment_system` injects both. Promotion is human-gated graduation local → shared.

Isolation: the autouse conftest `learnings_root` fixture points LEARNINGS_ROOT at a temp
dir; `_shared_root()` derives from it, so the real committed files are never touched here.
"""

from tools import learnings as L


# ── shared tier load/promote (pure store behavior) ───────────────────────────

def test_load_shared_empty_when_absent():
    assert L.load_shared_learnings("engineer") == ""


def test_promote_writes_to_shared_not_local():
    assert L.promote_learning("engineer", "verify the app boots on the real backend") is True
    assert "boots on the real backend" in L.load_shared_learnings("engineer")
    assert L.load_learnings("engineer") == ""          # the LOCAL store is untouched


def test_promote_dedupes_validates_and_guards_agent():
    assert L.promote_learning("engineer", "pin every dependency version") is True
    assert L.promote_learning("engineer", "pin every dependency version") is False   # duplicate
    assert L.promote_learning("engineer", "no") is False                              # too short
    assert L.promote_learning("not_an_agent", "a lesson long enough to record") is False


def test_promote_caps_oldest_first():
    for i in range(400):
        L.promote_learning("qa", f"lesson number {i} with enough words to matter here")
    text = L.load_shared_learnings("qa")
    assert len(text) <= L.MAX_SHARED_LEARNINGS_CHARS
    assert "lesson number 399" in text and "lesson number 0 " not in text


# ── augment_system injects both tiers ────────────────────────────────────────

def test_augment_injects_both_tiers_shared_first():
    L.promote_learning("qa", "test intent at the API level, not engine internals")
    L.record_learning("qa", "clean fixtures between e2e runs")
    out = L.augment_system("SYSTEM", "qa")
    assert "Shared learnings" in out and "API level" in out
    assert "Learnings from past runs" in out and "clean fixtures" in out
    assert out.index("Shared learnings") < out.index("Learnings from past runs")  # shared first


def test_augment_unchanged_when_no_learnings():
    assert L.augment_system("SYSTEM", "design") == "SYSTEM"


def test_augment_shared_only_and_local_only():
    L.promote_learning("design", "authorize every state-changing op at its boundary")
    only_shared = L.augment_system("SYS", "design")
    assert "Shared learnings" in only_shared and "Learnings from past runs" not in only_shared

    L.record_learning("architect", "name the enforcement point in the spec")
    only_local = L.augment_system("SYS", "architect")
    assert "Learnings from past runs" in only_local and "Shared learnings" not in only_local


# ── graduation: promote-by-index removes the local candidate ─────────────────

def test_graduate_removes_local_candidate():
    L.record_learning("architect", "lesson one alpha bravo charlie")
    L.record_learning("architect", "lesson two delta echo foxtrot")
    assert L.local_learning("architect", 0).startswith("lesson one")
    assert L.promote_learning("architect", "GENERIC: lesson one, stack-agnostic") is True
    assert L.remove_local_learning("architect", 0).startswith("lesson one")
    local = L.load_learnings("architect")
    assert "lesson one" not in local and "lesson two" in local            # only #0 graduated
    assert "GENERIC: lesson one" in L.load_shared_learnings("architect")


def test_local_learning_out_of_range_is_none():
    assert L.local_learning("engineer", 0) is None
    assert L.remove_local_learning("engineer", 5) is None


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_promote_text(capsys):
    rc = L.main(["promote", "--agent", "engineer", "--text",
                 "honor the framework's empty-body-response contract"])
    assert rc == 0
    assert "empty-body-response" in L.load_shared_learnings("engineer")
    assert "stack-agnostic" in capsys.readouterr().out.lower()             # the reminder prints


def test_cli_promote_index_graduates_with_rewrite():
    L.record_learning("engineer", "raw candidate lesson uno dos tres cuatro")
    rc = L.main(["promote", "--agent", "engineer", "--index", "0",
                 "--as", "Generic principle. (Default stack: …)"])
    assert rc == 0
    assert "Generic principle" in L.load_shared_learnings("engineer")
    assert "raw candidate" not in L.load_learnings("engineer")             # graduated out of local


def test_cli_list_shows_candidates_and_shared(capsys):
    L.record_learning("qa", "some candidate lesson here for listing")
    L.promote_learning("qa", "a committed shared lesson for listing")
    rc = L.main(["list", "--agent", "qa"])
    out = capsys.readouterr().out
    assert rc == 0 and "candidate" in out and "shared" in out.lower()


def test_cli_unknown_agent_and_bad_index_return_errors(capsys):
    assert L.main(["promote", "--agent", "nobody", "--text", "a lesson long enough"]) == 2
    assert L.main(["promote", "--agent", "engineer", "--index", "9"]) == 2  # no such candidate
