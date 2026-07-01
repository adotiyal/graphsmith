"""
DESIGN DECISION: One function owns all LLM calls.
- Every agent goes through here. No agent instantiates its own client.
- max_tokens is explicit on every call. Never let it default.
- System prompt + one user message only. No multi-turn history per agent.

PHASE 0 CHANGES:
- Prompt caching (0.1): the system block (identity + skill) is stable per agent
  and is now sent as a cached block. Cache reads are ~0.1x input cost. The same
  system prompt is reused across an agent's clarification re-runs and across
  Engineer retries, so this is a real, near-free saving.
- Model/token allocation: TWO models, split by WORKLOAD (CEO/CTO decision 2026-06-27).
    THINKING / DECISION / ANALYSIS → Opus 4.8   (tiers `fast` + `reason`)
    HANDS-ON CODING                → Sonnet 5    (tier `strong`)
  The tier KEYS are kept (fast/strong/reason) so call sites and tests don't churn;
  only what each maps to changed. See MODELS below for the per-agent split.
"""

import anthropic
import base64
import json
import os
import re
import time

from tools import trace

_client = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


# CEO/CTO model allocation (updated 2026-06-27): TWO models, split by WORKLOAD.
#   Opus 4.8  = deep thinking / decision-making / analysis
#   Sonnet 5  = hands-on coding
# The `fast` tier no longer means Haiku (retired) — it now runs Opus for the lighter
# DECISION/ANALYSIS agents (CEO, PM, Triage, QA review+diagnosis, peer consults, retro).
# `reason` runs Opus for the HEAVY thinking (Architect, Critic) AND the analysis work that
# must NOT ride the coding model: the Test Author (it IS the correctness oracle), the Design
# SPEC reasoning, and the design_qa VISION verdict. `strong` runs Sonnet for pure code
# emission (Engineer, Design kit/mockup, QA e2e specs, DevOps config). On the claude-cli
# (subscription) backend both models cost nothing extra — plan quota only.
MODELS = {
    "fast":   "claude-opus-4-8",     # DECISION/ANALYSIS: CEO, PM, Triage, QA review+diagnosis, consult, retro
    "strong": "claude-sonnet-5",     # CODING: Engineer, Design kit/mockup, QA e2e specs, DevOps config
    "reason": "claude-opus-4-8",     # DEEP THINKING + oracle: Architect, Critic, Test Author, Design spec, design_qa vision
}

# All tiers now share 8192: `fast` rose from 2048 (Haiku-sized) because it now hosts full
# Opus analysis outputs (a PRD / QA report truncates at 2048), and `reason` rose from 4096
# because it now hosts the Test Author's whole test suite and the Design spec. max_tokens is
# a CEILING (no cost for short outputs like triage/consult), bounded by CLAUDE_CODE_MAX_OUTPUT_TOKENS.
MAX_TOKENS = {
    "fast":   8192,
    "strong": 8192,
    "reason": 8192,
}

# Adaptive thinking (OPT-IN, 2026 capability). On Opus/Sonnet 4.6+ you set an EFFORT level
# and the model self-budgets its reasoning — more on hard tasks — with interleaved thinking
# (reason between tool results) on automatically; `budget_tokens` is deprecated on 4.7+.
# This maps the MOST thinking onto the highest-leverage reasoning (architect/critic) and the
# LEAST onto the cost-floor agents. Enable with `LLM_THINKING=adaptive`; DEFAULT (unset) keeps
# the exact current behavior. SAFE BY DESIGN: if the backend/SDK rejects the param the call
# falls back to the plain request (see _api_call), so turning this on can never break a run —
# but DO verify reasoning quality on a live run before relying on it. CLI-backend support is a
# follow-up (no verified `claude -p` effort flag yet); today the knob applies on LLM_BACKEND=api.
EFFORT = {
    "fast":   "standard",   # Opus for lighter decisions: CEO, PM, Triage, QA review, consult, retro
    "strong": "high",       # Sonnet code generation
    "reason": "high",       # Opus deep thinking + oracle (raise to "xhigh" if quota allows)
}


def _thinking(tier: str):
    """The adaptive-thinking block for a tier, or None when the feature is off (default)."""
    if os.environ.get("LLM_THINKING", "").strip().lower() != "adaptive":
        return None
    return {"type": "adaptive", "effort": EFFORT.get(tier, "standard")}


# Web search (§4.2, OPT-IN). The THINKING/spec agents (architect, surveyor) pin library
# versions + API shapes from training-cutoff memory; web search lets them VERIFY current
# versions, deprecations, and CVEs before committing the spec. DEFAULT OFF (LLM_WEB_SEARCH
# unset) = exact current behavior. SAFE BY DESIGN: call_llm falls back to a plain
# (memory-grounded) call if the search wiring fails, so enabling it can never break a run —
# but VERIFY spec quality on a live run before relying on it (the CLI WebSearch tool path
# can't be exercised from a sandbox). On the api backend it uses the `web_search` server tool.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


def _web_search_active() -> bool:
    """True when LLM_WEB_SEARCH opts in. Default off → no behavior change."""
    return os.environ.get("LLM_WEB_SEARCH", "").strip().lower() in ("1", "true", "on", "yes")


def _image_block(path: str) -> dict:
    """Base64 image content block for the Messages API (PNG/JPEG by extension)."""
    media = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def call_llm(
    system_prompt: str,
    user_message: str,
    tier: str = "fast",
    images: list = None,
    web_search: bool = False,
) -> str:
    """
    DESIGN DECISION: user_message is assembled by the agent, not passed raw
    from state. Agents select only what they need from disk.

    The system prompt is sent as a cached block (prompt caching). It is stable
    per agent, so subsequent calls with the same system prompt (clarification
    re-runs, Engineer retries) read from cache instead of re-billing input.

    images: optional list of (label, png_path) — each image is sent labeled
    before the text, for vision tasks (design QA compares app vs mockup).

    BACKENDS (cost control): LLM_BACKEND env selects how calls are billed.
    - "claude-cli" (DEFAULT, CEO decision 2026-06-12 — the API "costs extra"):
      headless `claude -p` — same auth as Claude Desktop/Code, billed to the
      SUBSCRIPTION (zero marginal $, bounded by plan quota). Vision calls route
      through the CLI too: image paths are passed and read via the CLI's Read tool.
    - "api": metered Anthropic API via ANTHROPIC_API_KEY (set LLM_BACKEND=api).
    """
    backend = os.environ.get("LLM_BACKEND", "claude-cli").strip().lower()
    # Web search only fires when the caller asks AND it's opted in AND this isn't a vision
    # call (the image path uses its own Read-tool flow; don't mix the two).
    use_search = web_search and _web_search_active() and not images
    t0 = time.perf_counter()

    def _do(search: bool):
        if backend == "claude-cli":
            return _cli_call(system_prompt, user_message, MODELS[tier],
                             images=images, web_search=search)
        return _api_call(system_prompt, user_message, tier, images, web_search=search)

    try:
        text, in_tok, out_tok = _do(use_search)
    except Exception:
        if not use_search:
            raise
        # The search wiring failed — fall back to a plain (memory-grounded) call so that
        # enabling web search can NEVER break a run. Worst case = no improvement.
        text, in_tok, out_tok = _do(False)

    # Observability: record the call (tokens/latency) into the active trace, if any.
    try:
        trace.emit(
            "llm_call", tier=tier, backend=backend, node=trace.current_node(),
            latency_ms=round((time.perf_counter() - t0) * 1000),
            in_tokens=in_tok, out_tokens=out_tok, out_chars=len(text),
            web_search=use_search,
        )
    except Exception:
        pass

    return text


# ── Structured control-plane signals (§4.1) ─────────────────────────────────
# The routing-critical signals (triage class, critic verdict, design-QA verdict) were
# parsed out of the model's PROSE with ad-hoc regexes — a misparse was a misroute. These
# turn a PURE-DECISION call into a VALIDATED object: the model is told to emit ONLY a JSON
# object, the JSON is extracted robustly (fence-/prose-tolerant, quote-aware), validated +
# coerced against a lightweight dependency-free schema, retried once with a corrective on
# failure, and a SAFE DEFAULT is returned if the model never complies — so the routing
# layer stops guessing and a parse failure is an explicit, traced fallback, not a silent
# misroute. NOT for marker-in-artifact calls (NEEDS_INPUT, the QA sign-off verdict): there
# the call's primary output is a large artifact, so JSON-only would destroy it — those keep
# robust marker extraction. Works on BOTH backends (it rides call_llm).


def _strip_fences(text: str) -> str:
    """Drop a leading ```json / ``` fence wrapper if the whole reply is fenced."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


def _json_objects(text: str) -> list:
    """All top-level balanced {...} spans in `text`, quote-aware (braces inside double-
    quoted strings don't change depth; backslash escapes respected). Pure/testable."""
    spans, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start: i + 1])
    return spans


def _extract_json(text: str):
    """Best-effort dict out of a model reply: try the whole (fence-stripped) reply, then
    the LAST balanced object that parses (a decision usually trails any prose). None if
    no JSON dict is present."""
    stripped = _strip_fences(text)
    for candidate in [stripped, *reversed(_json_objects(stripped))]:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _coerce_schema(data: dict, schema: dict):
    """Validate/coerce `data` against the lightweight schema → (ok, coerced). Each field:
    {"type": "enum"|"string"|"bool"|"int", "values": [...], "required": bool}. enum match
    is case-insensitive and returns the CANONICAL listed value. ok=False if a required
    field is missing or an enum value is out of range."""
    out = {}
    for field, spec in schema.items():
        required = spec.get("required", False)
        if field not in data or data[field] is None:
            if required:
                return False, out
            out[field] = None
            continue
        val = data[field]
        kind = spec.get("type", "string")
        if kind == "enum":
            norm = str(val).strip().lower()
            match = next((v for v in spec["values"] if v.lower() == norm), None)
            if match is None:
                if required:
                    return False, out
                out[field] = None
            else:
                out[field] = match
        elif kind == "bool":
            out[field] = val if isinstance(val, bool) else str(val).strip().lower() in (
                "true", "yes", "1")
        elif kind == "int":
            try:
                out[field] = int(val)
            except (ValueError, TypeError):
                if required:
                    return False, out
                out[field] = None
        else:                                            # string
            out[field] = str(val)
    return True, out


def _schema_instruction(schema: dict) -> str:
    """A strict 'emit ONLY this JSON' contract describing the schema's exact shape."""
    fields = []
    for field, spec in schema.items():
        kind = spec.get("type", "string")
        if kind == "enum":
            sample = " | ".join(spec["values"])
        elif kind == "bool":
            sample = "true | false"
        elif kind == "int":
            sample = "<integer>"
        else:
            sample = "<string or null>"
        opt = "" if spec.get("required") else "   (optional, null if N/A)"
        fields.append(f'  "{field}": {sample}{opt}')
    return ("\n\nRespond with ONLY a JSON object of EXACTLY this shape — no prose, no "
            "markdown fences, nothing else:\n{\n" + ",\n".join(fields) + "\n}")


def call_structured(system_prompt: str, user_message: str, schema: dict,
                    tier: str = "fast", images: list = None,
                    default: dict = None, retries: int = 1) -> dict:
    """Get a VALIDATED structured decision from the model (see the section note above).
    Returns a dict guaranteed to satisfy `schema`, or `default` if the model never
    complies after `retries` corrective attempts. Rides call_llm (caching/tracing/tier)."""
    instruction = _schema_instruction(schema)
    msg = user_message + instruction
    reason = ""
    for _ in range(retries + 1):
        raw = call_llm(system_prompt, msg, tier=tier, images=images)
        data = _extract_json(raw)
        if data is None:
            reason = "no JSON object found"
        else:
            ok, coerced = _coerce_schema(data, schema)
            if ok:
                return coerced
            reason = "missing or invalid required field(s)"
        msg = (user_message + instruction +
               f"\n\nYour previous reply was rejected ({reason}). Output ONLY the JSON "
               "object specified above and nothing else.")
    try:
        trace.emit("structured_fallback", fields=list(schema.keys()), reason=reason)
    except Exception:
        pass
    return dict(default) if default else {}


def _api_call(system_prompt: str, user_message: str, tier: str, images: list = None,
              web_search: bool = False):
    client = get_client()
    system_blocks = [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]
    if images:
        content = []
        for label, path in images:
            content.append({"type": "text", "text": label})
            content.append(_image_block(path))
        content.append({"type": "text", "text": user_message})
    else:
        content = user_message
    base = dict(
        model=MODELS[tier],
        max_tokens=MAX_TOKENS[tier],
        system=system_blocks,
        messages=[{"role": "user", "content": content}],
    )
    full = dict(base)
    if web_search:
        full["tools"] = [WEB_SEARCH_TOOL]    # server-side tool — searches run server-side
    think = _thinking(tier)
    if think:
        full["thinking"] = think
    try:
        response = client.messages.create(**full)
    except Exception:
        # An unsupported param (thinking and/or tools) → drop the extras and retry plain,
        # so neither opt-in feature can break the call.
        response = client.messages.create(**base)
    usage = getattr(response, "usage", None)
    # Robust to thinking blocks: collect TEXT blocks (skip thinking/tool blocks). With web
    # search the FINAL text block is the answer (after the search turns); otherwise the
    # first text block. Falls back to "" if none.
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    text = (texts[-1] if web_search and texts else (texts[0] if texts else ""))
    return (text, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0))


_CLI_BIN = None


def _find_claude_bin() -> str:
    """Resolve the claude CLI binary: explicit env → PATH → common install homes
    (nvm puts it under a node-version bin that non-login shells don't see)."""
    global _CLI_BIN
    if _CLI_BIN:
        return _CLI_BIN
    import glob
    import shutil
    candidates = [os.environ.get("CLAUDE_CLI_BIN"), shutil.which("claude")]
    home = os.path.expanduser("~")
    candidates += sorted(glob.glob(f"{home}/.nvm/versions/node/*/bin/claude"), reverse=True)
    candidates += [f"{home}/.local/bin/claude", f"{home}/.claude/local/claude",
                   "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            _CLI_BIN = c
            return c
    raise RuntimeError(
        "LLM_BACKEND=claude-cli but no `claude` binary found. Install with "
        "`npm install -g @anthropic-ai/claude-code` (+ login), set CLAUDE_CLI_BIN to "
        "its path, or set LLM_BACKEND=api to use the metered API.")


_CLI_WORKDIR = None

# Claude Code's own harness prompt biases toward concise chat answers and tool use —
# both poison for pipeline generation (we need COMPLETE files as plain text). These
# guards restore API-call semantics; quality must be ≥ the api backend, never less.
_TEXT_GUARD = (
    "\n\nONE-SHOT COMPLETION RULES (these override any default behavior): "
    "Tools are DISABLED for this task — never invoke a tool; respond with plain text "
    "only. Produce the COMPLETE, full-length output the task specifies — never "
    "summarize, never truncate, never abbreviate file contents, and add no preamble, "
    "commentary, or markdown fences beyond the requested format.")
_VISION_GUARD = (
    "\n\nAfter Reading the image files, respond with PLAIN TEXT ONLY in the requested "
    "format — complete and unabridged; use no tool other than Read.")
_SEARCH_GUARD = (
    "\n\nYou MAY use the WebSearch tool to VERIFY current facts before relying on them — "
    "library/framework versions, API shapes, deprecations, and CVEs — rather than trusting "
    "training-cutoff memory for anything version-specific. Use no other tool. When finished "
    "searching, respond with the COMPLETE, full-length plain-text output the task specifies "
    "— no preamble, no commentary, and no markdown fences beyond the requested format.")
_RETRY_GUARD = (
    "\n\nIMPORTANT: your previous attempt FAILED because you invoked tools until the "
    "turn budget ran out. Do not call any tool you do not strictly need — produce the "
    "final plain-text answer NOW.")


def _cli_workdir() -> str:
    """A neutral empty cwd for CLI calls: running from the pipeline repo would load
    Graphsmith's own CLAUDE.md (+ repo context) into every call — pure token
    overhead and behavioral contamination for what should be a clean completion."""
    global _CLI_WORKDIR
    if _CLI_WORKDIR is None or not os.path.isdir(_CLI_WORKDIR):
        import tempfile
        _CLI_WORKDIR = tempfile.mkdtemp(prefix="graphsmith-llm-")
    return _CLI_WORKDIR


def _cli_call(system_prompt: str, user_message: str, model: str,
              timeout: int = 1800, images: list = None, web_search: bool = False,
              _retry: bool = True) -> tuple:
    # timeout 900→1800 (live): phase-2 e2e authoring (multi-file, big context) blew
    # past 15 min on the strong tier and crashed the QA node mid-graph.
    """One completion through headless Claude Code (`claude -p`) — subscription-billed.
    Text calls: small turn budget (a stray tool attempt self-recovers — a live QA
    consult on the fast tier ignored the no-tools guard at --max-turns 1 and hard-
    crashed the node). Vision calls: image paths are passed in the prompt and the
    CLI reads them with its own Read tool. On error_max_turns the call retries ONCE
    with a doubled budget + explicit retry guard. Slimmed + quality-guarded:
    neutral cwd (no CLAUDE.md), no MCP servers, output ceiling above the api
    backend's, anti-brevity/anti-tool-use system guard."""
    import json as json_mod
    import shutil
    import subprocess
    import uuid
    prompt = user_message
    workdir = _cli_workdir()
    img_dir = None
    boost = 1 if _retry else 2     # the retry runs with a doubled turn budget
    cmd = [_find_claude_bin(), "--model", model,
           "--output-format", "json", "--strict-mcp-config"]
    if images:
        # Copy images INTO the neutral cwd: Reads of outside-cwd paths hit
        # permission friction and burn the turn budget (live error_max_turns).
        img_dir = os.path.join(workdir, f"imgs-{uuid.uuid4().hex[:8]}")
        os.makedirs(img_dir)
        local = []
        for n, (label, path) in enumerate(images):
            dst = os.path.join(img_dir, f"img{n}{os.path.splitext(path)[1] or '.png'}")
            shutil.copyfile(path, dst)
            local.append((label, dst))
        labeled = "\n".join(f"{label} {path}" for label, path in local)
        prompt = (f"First Read each of these image files (they are screenshots you must "
                  f"visually inspect):\n{labeled}\n\n{user_message}")
        cmd += ["--append-system-prompt", system_prompt + _VISION_GUARD,
                "--max-turns", str((3 + 2 * len(images)) * boost),
                "--allowed-tools", "Read", "--permission-mode", "acceptEdits"]
    elif web_search:
        # WebSearch needs turns to search THEN answer; allow ONLY WebSearch (no MCP via
        # --strict-mcp-config above). On error the caller falls back to a plain call.
        cmd += ["--append-system-prompt", system_prompt + _SEARCH_GUARD
                + ("" if _retry else _RETRY_GUARD),
                "--max-turns", str(8 * boost),
                "--allowed-tools", "WebSearch", "--permission-mode", "acceptEdits"]
    else:
        cmd += ["--append-system-prompt", system_prompt + _TEXT_GUARD
                + ("" if _retry else _RETRY_GUARD),
                "--max-turns", str(4 * boost)]
    # The prompt goes via STDIN, not argv: a live ~200KB authoring prompt on argv
    # made the CLI stall hunting for stdin ("no stdin data received in 3s") and fail.
    cmd += ["-p"]
    env = dict(os.environ)
    # generation headroom ABOVE the api tiers (engineer truncation was a real bug class)
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "32000")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=env, input=prompt)
    finally:
        if img_dir:
            shutil.rmtree(img_dir, ignore_errors=True)
    try:
        out = json_mod.loads(r.stdout) if r.stdout else {}
    except ValueError:
        out = {}
    if r.returncode != 0 or out.get("is_error"):
        if out.get("subtype") == "error_max_turns" and _retry:
            return _cli_call(system_prompt, user_message, model,
                             timeout=timeout, images=images, web_search=web_search,
                             _retry=False)
        raise RuntimeError(f"claude-cli call failed: {(r.stderr or r.stdout)[:500]}")
    usage = out.get("usage") or {}
    return (out.get("result", ""),
            usage.get("input_tokens", 0), usage.get("output_tokens", 0))
