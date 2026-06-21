"""
Kit testid SUFFIX-resolution (live phase-2 TrailTribe regression).

A kit component can re-emit its `data-testid` prop with a STATE suffix
(`data-testid={`${testId}-add-friend`}`) — so it renders `<base>-<suffix>`, NEVER the
bare base. The old string-grep tooling surfaced the bare base to QA as a "real" selector
AND let a base-only e2e assertion pass the contract gate (the base string IS in the
source, as the prop literal handed to the suffix-renderer). Three e2e tests then failed
at the most expensive verification stage. These pin the suffix-aware behavior:
  - RelationshipButton-style component  → suffixes resolved, bare base suppressed
  - FollowButton-style component        → forwards verbatim, base preserved
"""

from tools import registry


# leaf suffix-renderer (cf. RelationshipButton): base in, `${base}-<state>` out
REL = '''import { Button } from "./Button";
export function Rel({ state, "data-testid": testId = "rel" }) {
  if (state === "friend")
    return <Button data-testid={`${testId}-friends`}>Friends</Button>;
  if (state === "pending")
    return <Button data-testid={`${testId}-requested`}>Requested</Button>;
  return <Button data-testid={`${testId}-add-friend`}>Add friend</Button>;
}
'''

# verbatim forwarder (cf. FollowButton): base in, the SAME base out
FOLLOW = '''import { Button } from "./Button";
export function Follow({ following, "data-testid": testId = "follow-toggle" }) {
  return <Button data-testid={testId}>{following ? "Following" : "Follow"}</Button>;
}
'''

# consumers passing a base INTO the suffix-renderer (cf. ProfileView / AdventurerCard)
PROFILE = '''import { Rel } from "./Rel";
import { Follow } from "./Follow";
export function Profile({ user }) {
  return (
    <div>
      <Rel state={user.rel} data-testid="profile-relationship" />
      <Follow following={user.following} data-testid="profile-follow" />
    </div>
  );
}
'''

CARD = '''import { Rel } from "./Rel";
export function Card({ username, rel }) {
  return <Rel state={rel} data-testid={`card-relationship-${username}`} />;
}
'''

# HYBRID (cf. UsernameField): forwards the base VERBATIM onto the real input AND emits
# state-suffixed sub-elements — so BOTH the bare base and the suffixed ids render.
FIELD = '''import { TextField } from "./TextField";
export function Field({ id, "data-testid": testId = "username-field", status }) {
  return (
    <div>
      {status === "checking" && <span data-testid={`${testId}-checking`}>Checking…</span>}
      {status === "available" && <span data-testid={`${testId}-available`}>available</span>}
      <TextField id={id} data-testid={testId} />
    </div>
  );
}
'''

FORM = '''import { Field } from "./Field";
export function Form({ status }) {
  return <Field id="onboarding-username" data-testid="onboarding-username" status={status} />;
}
'''


def _kit(tmp_path, files):
    d = tmp_path / "frontend" / "src" / "components" / "kit"
    d.mkdir(parents=True)
    for name, content in files.items():
        (d / name).write_text(content)
    return d


def test_extract_resolves_suffix_renderer_and_suppresses_bare_base(tmp_path):
    kit = _kit(tmp_path, {"Rel.tsx": REL, "Follow.tsx": FOLLOW,
                          "Profile.tsx": PROFILE, "Card.tsx": CARD})
    ids, prefixes, _ = registry.extract_kit_interface(kit, "")

    # the suffix-renderer's REAL rendered ids = consumer base + each state suffix
    assert {"profile-relationship-add-friend", "profile-relationship-requested",
            "profile-relationship-friends"} <= ids
    # …plus its default-base variants (pins the suffix SET against a silent rename)
    assert {"rel-add-friend", "rel-requested", "rel-friends"} <= ids
    # the BARE base no element ever renders is NOT surfaced (this was the bug)
    assert "profile-relationship" not in ids
    # the verbatim forwarder's base is preserved as-is (its rendered id == the base)
    assert "profile-follow" in ids
    # the dynamic consumer base becomes a prefix: card-relationship-<user>-<state>
    assert "card-relationship-" in prefixes
    assert "card-relationship" not in ids


def test_hybrid_verbatim_plus_suffix_keeps_bare_base(tmp_path):
    # UsernameField class: a component that forwards the base verbatim onto the real input
    # AND emits state-suffixed sub-elements. The bare base renders, so it must NOT be
    # suppressed (suppressing it was a false-positive: the live `onboarding-username` input).
    kit = _kit(tmp_path, {"Field.tsx": FIELD, "Form.tsx": FORM})
    ids, _prefixes, _ = registry.extract_kit_interface(kit, "")
    assert "onboarding-username" in ids                       # the verbatim input id
    assert {"onboarding-username-checking", "onboarding-username-available"} <= ids
    # default-base variants also keep the bare base for a hybrid
    assert {"username-field", "username-field-checking"} <= ids

    # and the contract gate accepts a spec that fills the bare base (real input)
    e2e = tmp_path / "e2e"
    e2e.mkdir()
    (e2e / "test_u.py").write_text('page.get_by_test_id("onboarding-username").fill("x")\n')
    ok, msg = registry.check_testid_contract(str(tmp_path))
    assert ok, msg


def test_state_suffix_map_exposes_variants_for_qa(tmp_path):
    kit = _kit(tmp_path, {"Rel.tsx": REL, "Follow.tsx": FOLLOW})
    sources = {p.name: p.read_text() for p in kit.glob("*.tsx")}
    suffixes = registry.kit_state_suffixes(sources)
    assert suffixes.get("Rel") == ["-add-friend", "-friends", "-requested"]
    assert "Follow" not in suffixes          # verbatim forwarder is NOT a suffix-renderer


def test_contract_gate_flags_base_only_assertion(tmp_path):
    _kit(tmp_path, {"Rel.tsx": REL, "Follow.tsx": FOLLOW,
                    "Profile.tsx": PROFILE, "Card.tsx": CARD})
    e2e = tmp_path / "e2e"
    e2e.mkdir()

    # the BUGGY spec asserts the bare base that no element renders → must be FLAGGED
    bad = e2e / "test_bad.py"
    bad.write_text('page.get_by_test_id("profile-relationship")\n')
    ok, msg = registry.check_testid_contract(str(tmp_path))
    assert not ok and "profile-relationship" in msg

    # the FIXED spec asserts the real suffixed ids (static + dynamic) → must PASS
    bad.unlink()
    (e2e / "test_ok.py").write_text(
        'page.get_by_test_id("profile-relationship-add-friend")\n'
        'page.get_by_test_id(f"card-relationship-{u}-requested")\n')
    ok, msg = registry.check_testid_contract(str(tmp_path))
    assert ok, msg


def test_lint_rejects_base_only_selector(tmp_path):
    # QA-authoring guard: with the resolved kit interface, a bare-base selector is flagged
    # while the real suffixed id is accepted.
    kit = _kit(tmp_path, {"Rel.tsx": REL, "Profile.tsx": PROFILE})
    sources = {p.name: p.read_text() for p in kit.glob("*.tsx")}
    static, prefixes = registry.resolve_kit_testids(sources)

    bad = registry.lint_e2e_spec('page.get_by_test_id("profile-relationship")\n',
                                 kit_testids=static, testid_prefixes=tuple(prefixes))
    assert any("profile-relationship" in f for f in bad)
    good = registry.lint_e2e_spec('page.get_by_test_id("profile-relationship-add-friend")\n',
                                  kit_testids=static, testid_prefixes=tuple(prefixes))
    assert good == []
