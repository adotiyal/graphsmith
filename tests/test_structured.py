"""
Structured control-plane signals (§4.1) — the validated-decision helper that replaces
ad-hoc regex parsing of the model's prose for the routing-critical signals (triage class,
critic verdict, design-QA verdict).

The pure pieces (JSON extraction, schema coercion) are tested deterministically; the
`call_structured` orchestration is tested through the LLM mock (valid first try, retry
then succeed, safe default on persistent failure, image passthrough).
"""

from tools import llm
from tools.llm import (
    call_structured, _extract_json, _coerce_schema, _json_objects,
    _strip_fences, _schema_instruction,
)


# ── _json_objects / _strip_fences / _extract_json (pure) ─────────────────────

def test_json_objects_quote_aware():
    assert _json_objects('{"a":1} text {"b":2}') == ['{"a":1}', '{"b":2}']
    # a brace INSIDE a string value must not split the object
    assert _json_objects('{"gaps":"use {x} here"}') == ['{"gaps":"use {x} here"}']


def test_strip_fences():
    assert _strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_fences('```\n{"a":1}\n```') == '{"a":1}'
    assert _strip_fences('{"a":1}') == '{"a":1}'


def test_extract_json_bare_fenced_and_prose_wrapped():
    assert _extract_json('{"verdict":"pass"}') == {"verdict": "pass"}
    assert _extract_json('```json\n{"verdict":"fail"}\n```') == {"verdict": "fail"}
    # decision trails prose → the LAST balanced object wins
    assert _extract_json('findings here...\n{"verdict":"MISALIGNED"}') == {"verdict": "MISALIGNED"}
    assert _extract_json('{"a":1} noise {"b":2}') == {"b": 2}
    assert _extract_json('{"gaps":"use {x}"}') == {"gaps": "use {x}"}


def test_extract_json_none_on_garbage():
    assert _extract_json("no json here at all") is None
    assert _extract_json("") is None


# ── _coerce_schema (pure) ────────────────────────────────────────────────────

_VERDICT = {"verdict": {"type": "enum", "values": ["pass", "fail"], "required": True},
            "gaps": {"type": "string", "required": False}}


def test_coerce_enum_is_case_insensitive_and_canonicalized():
    ok, out = _coerce_schema({"verdict": "PASS", "gaps": "x"}, _VERDICT)
    assert ok and out == {"verdict": "pass", "gaps": "x"}


def test_coerce_missing_required_fails():
    ok, _ = _coerce_schema({"gaps": "x"}, _VERDICT)
    assert ok is False


def test_coerce_invalid_enum_fails():
    ok, _ = _coerce_schema({"verdict": "maybe"}, _VERDICT)
    assert ok is False


def test_coerce_optional_missing_is_none_and_types_coerce():
    ok, out = _coerce_schema({"verdict": "fail"}, _VERDICT)
    assert ok and out["gaps"] is None
    schema = {"n": {"type": "int", "required": True}, "b": {"type": "bool", "required": True}}
    ok, out = _coerce_schema({"n": "7", "b": "true"}, schema)
    assert ok and out == {"n": 7, "b": True}


def test_schema_instruction_lists_fields_and_enum_options():
    instr = _schema_instruction(_VERDICT)
    assert "pass | fail" in instr and '"verdict"' in instr and "optional" in instr


# ── call_structured (through the LLM mock) ───────────────────────────────────

_CT = {"change_type": {"type": "enum",
                       "values": ["bugfix", "refactor", "chore", "feature"],
                       "required": True}}


def test_call_structured_valid_first_try(llm):
    llm.default = '{"change_type":"bugfix"}'
    assert call_structured("sys", "classify this", _CT) == {"change_type": "bugfix"}
    assert len(llm.calls) == 1                      # no retry needed
    # the strict JSON instruction is appended to the user message
    assert "ONLY a JSON object" in llm.calls[0]["user"]


def test_call_structured_retries_then_succeeds(llm):
    llm.queue = ["sorry, here is prose not json", '{"change_type":"chore"}']
    assert call_structured("sys", "classify", _CT) == {"change_type": "chore"}
    assert len(llm.calls) == 2                       # one corrective retry
    assert "rejected" in llm.calls[1]["user"]


def test_call_structured_falls_back_to_default(llm):
    llm.default = "never valid json"
    out = call_structured("sys", "classify", _CT, default={"change_type": "feature"})
    assert out == {"change_type": "feature"}         # safe default, not a misroute
    assert len(llm.calls) == 2                        # tried + one retry, then defaulted


def test_call_structured_passes_images_through(llm):
    llm.default = '{"verdict":"ALIGNED"}'
    schema = {"verdict": {"type": "enum", "values": ["ALIGNED", "MISALIGNED"], "required": True}}
    out = call_structured("sys", "compare", schema, tier="strong",
                          images=[("app", "/tmp/a.png")])
    assert out == {"verdict": "ALIGNED"}
    assert llm.calls[-1]["images"] == [("app", "/tmp/a.png")]
    assert llm.calls[-1]["tier"] == "strong"
