"""
Adaptive-thinking opt-in (LLM_THINKING=adaptive).

Verifies the effort mapping and — critically — that the feature is OFF by default, so
enabling it is a deliberate, separately-verified step that can't silently change behavior.
The actual API call path falls back to a plain request if the backend rejects the param
(see tools/llm._api_call), so this never destabilizes a run.
"""

from tools import llm


def test_thinking_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_THINKING", raising=False)
    assert llm._thinking("strong") is None
    assert llm._thinking("reason") is None


def test_thinking_on_maps_effort(monkeypatch):
    monkeypatch.setenv("LLM_THINKING", "adaptive")
    assert llm._thinking("reason") == {"type": "adaptive", "effort": "high"}
    assert llm._thinking("strong") == {"type": "adaptive", "effort": "high"}
    assert llm._thinking("fast") == {"type": "adaptive", "effort": "standard"}
    assert llm._thinking("nonexistent-tier")["effort"] == "standard"   # safe default


def test_every_tier_has_an_effort_level():
    assert set(llm.EFFORT) == set(llm.MODELS)
