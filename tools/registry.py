"""
tools/registry.py
-----------------
DESIGN DECISION: Tools are plain Python functions, not LangChain tools.
- No framework dependency for tools.
- Each agent imports only the tools it needs — no global registry object.
- Tools are synchronous and return (success: bool, output: str).
  Simple tuple. Agents check success before proceeding.

DESIGN DECISION: Tools do real work, not LLM calls.
- LLM calls live in llm.py. Tools are deterministic executors.
- Agent logic decides WHEN to call a tool.
- Tool decides HOW to do the work.

Current tools per agent:
  Design agent    → validate_components (checks names against shadcn list)
  Architect agent → validate_api_spec (checks REST conventions)
  Engineer agent  → run_linter, run_tests (Docker)
  QA agent        → run_tests (reused from engineer)

BLOCKER WATCH: tool failures should not crash the pipeline.
Every tool returns (False, error_message) on failure — never raises.
"""

import ast
import os
import subprocess
import re
import json
import sys
from pathlib import Path

# Pinned slim/alpine base images — small AND reproducible (no floating :latest, which
# can break a build with no code change). Multi-stage Dockerfiles keep finals lean.
PY_IMAGE = "python:3.12-slim"
NODE_IMAGE = "node:22-alpine"
POSTGRES_IMAGE = "postgres:17-alpine"   # used by the compose integration stage (slice 2)

# ── Design tools ────────────────────────────────────────────────────────────

SHADCN_COMPONENTS = {
    "button", "input", "textarea", "select", "checkbox", "radiogroup",
    "switch", "dialog", "sheet", "tabs", "table", "card", "badge",
    "alert", "toast", "skeleton", "avatar", "dropdownmenu", "tooltip",
    "form",
}


def validate_components(design_spec: str) -> tuple[bool, str]:
    """
    Check that all component names in the design spec exist in shadcn/ui.
    Flags anything not in the list so architect/engineer aren't surprised.
    Simple word match — not perfect, but catches obvious drift.
    """
    # Extract capitalised words that look like component names
    candidates = re.findall(r'\b([A-Z][a-zA-Z]+)\b', design_spec)
    unknown = [
        c for c in candidates
        if c.lower() not in SHADCN_COMPONENTS
        and c not in {"React", "TypeScript", "Tailwind", "Postgres", "FastAPI",
                      "SQLAlchemy", "UUID", "CORS", "JWT", "HTTP", "API",
                      "PRD", "CEO", "PM", "QA", "UI", "UX", "TODO"}
    ]
    if unknown:
        return False, f"Possible non-shadcn components: {', '.join(set(unknown))}. Verify these are in the component library."
    return True, "All component names look valid."


# ── Architect tools ──────────────────────────────────────────────────────────

def validate_api_spec(tech_spec: str) -> tuple[bool, str]:
    """
    Basic REST convention check on the tech spec.
    Looks for verbs in paths (anti-pattern) and missing status codes.
    """
    issues = []

    # Check for verb-based paths
    verb_pattern = re.compile(r'/(get|post|put|delete|fetch|create|update|remove)[A-Z/]', re.IGNORECASE)
    if verb_pattern.search(tech_spec):
        issues.append("Verb-based URL paths detected (e.g. /getUser). Use nouns: /users.")

    # Check that endpoints table has HTTP methods
    if "GET|" not in tech_spec and "| GET" not in tech_spec and "GET /" not in tech_spec:
        issues.append("No HTTP methods found in endpoint definitions. Add Method column to endpoint table.")

    if issues:
        return False, "\n".join(issues)
    return True, "API spec looks clean."


# ── Engineer tools ───────────────────────────────────────────────────────────

# ── Security scan (Phase 2.3) ────────────────────────────────────────────────

# (regex, human label) — deterministic, dependency-free static checks on generated code.
_SECURITY_PATTERNS = [
    (r"\beval\s*\(", "use of eval()"),
    (r"\bexec\s*\(", "use of exec()"),
    (r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True", "subprocess with shell=True"),
    (r"\bos\.system\s*\(", "os.system() call"),
    (r"pickle\.loads?\s*\(", "pickle deserialization (RCE risk)"),
    (r"yaml\.load\s*\((?![^)]*Loader)", "yaml.load without SafeLoader"),
    (r"verify\s*=\s*False", "TLS verification disabled (verify=False)"),
    (r"(?i)(?:password|secret|api[_-]?key|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}['\"]",
     "possible hardcoded secret"),
]
_SCAN_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".yaml", ".yml", ".env"}


def scan_security(files) -> tuple[bool, list]:
    """
    Static security scan over the given file paths (absolute or Path).
    Returns (ok, issues) where issues is ["<file>: <label>", ...].
    Deterministic and dependency-free — runs AFTER code is written, like the linter.
    """
    compiled = [(re.compile(rx), label) for rx, label in _SECURITY_PATTERNS]
    issues = set()
    for f in files or []:
        p = Path(f)
        if not p.is_file() or p.suffix not in _SCAN_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rx, label in compiled:
            if rx.search(text):
                issues.add(f"{p.name}: {label}")
    return (len(issues) == 0, sorted(issues))


def run_linter(project_dir: str) -> tuple[bool, str]:
    """
    Run ruff linter on generated Python code.
    DESIGN DECISION: ruff over flake8/pylint — single binary, fast, zero config needed.
    BLOCKER: ruff must be installed: pip install ruff
    """
    result = subprocess.run(
        ["ruff", "check", project_dir, "--select=E,F", "--quiet"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stdout[:400]
    return True, "Linter passed."


# ── Code-quality layer (additive, non-blocking) ──────────────────────────────
# These AUGMENT run_linter (which stays the proven blocking E,F gate). They are
# either (a) AUTO-FIX steps that can only make code cleaner, or (b) ADVISORY reports
# surfaced to QA + the PR gate like scan_security — never a NEW hard gate that could
# destabilize the engineer⇄QA loop. Every function degrades gracefully when the
# underlying tool is absent and NEVER raises (registry contract, top of file).

# Auto-fixable, low-controversy rule families — safe to apply unattended:
#   E pycodestyle · F pyflakes (unused imports/vars) · I import sorting ·
#   UP pyupgrade · B bugbear (autofix subset) · SIM simplify.
_AUTOFIX_SELECT = "E,F,I,UP,B,SIM"
# Advisory analysis set (reported, never fixed/blocked) incl. mccabe complexity (C90).
_QUALITY_SELECT = "E,F,B,SIM,UP,C90"
_MAX_COMPLEXITY = 10


def format_code(project_dir: str) -> tuple[bool, str]:
    """
    Auto-fix + format generated Python: `ruff check --fix` (safe import-sort, pyupgrade
    and unused-import cleanup) then `ruff format`. Runs BEFORE the linter gate so the
    blocking E,F lint passes MORE often, leaving code consistently styled. Non-blocking:
    a failure or a missing ruff binary returns (False, reason) and the caller proceeds.
    """
    try:
        subprocess.run(
            ["ruff", "check", project_dir, f"--select={_AUTOFIX_SELECT}",
             "--fix", "--quiet"],
            capture_output=True, text=True, timeout=120,
        )
        fmt = subprocess.run(
            ["ruff", "format", project_dir],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return False, "ruff not installed — formatting skipped"
    except Exception as e:                      # never break the pipeline
        return False, f"format_code skipped: {e}"
    last = (fmt.stdout or "").strip().splitlines()
    return True, (last[-1] if last else "formatted")


def _parse_ruff_statistics(output: str) -> list:
    """Parse `ruff check --statistics` lines → [(count, code, message), ...] (pure/testable)."""
    out = []
    for line in output.splitlines():
        m = re.match(r"\s*(\d+)\s+([A-Z]+\d+)\s+(.*)", line)
        if m:
            out.append((int(m.group(1)), m.group(2), m.group(3).strip()))
    return out


def _mypy_error_count(output: str) -> int:
    """Parse mypy output → number of type errors (pure/testable)."""
    m = re.search(r"Found (\d+) error", output)
    if m:
        return int(m.group(1))
    return sum(1 for ln in output.splitlines() if ": error:" in ln)


def code_quality_report(project_dir: str, files=None) -> list:
    """
    ADVISORY (non-blocking) code-quality findings over the Python the engineer just
    wrote, surfaced to QA and the PR gate alongside scan_security. Covers ruff bug/style
    families + mccabe complexity (C90) and, when available, mypy static types. Scoped to
    `files` when given (so extend-mode runs never report a big repo's pre-existing debt).
    Returns human-readable strings; empty when clean or when no tooling is installed.
    Never raises.
    """
    py = [str(f) for f in (files or []) if str(f).endswith(".py") and Path(f).is_file()]
    targets = py or [project_dir]
    findings: list = []

    # 1) ruff advisory + complexity in a single pass.
    try:
        res = subprocess.run(
            ["ruff", "check", *targets, f"--select={_QUALITY_SELECT}",
             f"--max-complexity={_MAX_COMPLEXITY}", "--statistics", "--quiet"],
            capture_output=True, text=True, timeout=120,
        )
        stats = _parse_ruff_statistics(res.stdout)
    except Exception:
        stats = []
    complexity = sum(c for c, code, _ in stats if code.startswith("C9"))
    if complexity:
        findings.append(f"complexity: {complexity} function(s) over cyclomatic "
                        f"{_MAX_COMPLEXITY} — refactor for readability/debuggability")
    advisory = sorted((row for row in stats if not row[1].startswith("C9")), reverse=True)
    if advisory:
        top = ", ".join(f"{c}×{code}" for c, code, _ in advisory[:3])
        total = sum(c for c, _, _ in advisory)
        findings.append(f"lint(advisory): {total} issue(s) — {top}")

    # 2) mypy static type check (advisory, best-effort — false positives are fine here
    #    because this never gates; the roadmap proposes promoting it with proper config).
    try:
        res = subprocess.run(
            ["mypy", *targets, "--ignore-missing-imports", "--no-error-summary",
             "--hide-error-context", "--no-color-output"],
            capture_output=True, text=True, timeout=90,
        )
        n = _mypy_error_count(res.stdout)
        if n:
            findings.append(f"types: {n} mypy error(s) (advisory)")
    except Exception:
        pass
    return findings


_FRONTEND_ESLINT_FILES = (".eslintrc", ".eslintrc.json", ".eslintrc.js",
                          "eslint.config.js", "eslint.config.mjs")
_FRONTEND_PRETTIER_FILES = (".prettierrc", ".prettierrc.json", ".prettierrc.js",
                            "prettier.config.js")


def check_frontend_quality_tooling(project_dir: str) -> list:
    """
    Deterministic, dependency-free (no Node needed): if the generated app has a frontend
    (a package.json), verify it ships the quality tooling a clean TS/Next codebase needs —
    ESLint, Prettier, a strict tsconfig, and a typecheck script. Advisory findings only
    (non-blocking), surfaced like the other quality signals. Never raises.
    """
    root = Path(project_dir)
    pkg = None
    for cand in [root / "package.json", *sorted(root.glob("*/package.json"))]:
        if cand.is_file():
            pkg = cand
            break
    if pkg is None:
        return []                               # backend-only — nothing to check
    fe = pkg.parent
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    dev = {**(data.get("devDependencies") or {}), **(data.get("dependencies") or {})}
    scripts = data.get("scripts") or {}
    findings = []
    if "eslint" not in dev and not any((fe / n).exists() for n in _FRONTEND_ESLINT_FILES):
        findings.append("frontend: no ESLint configured (add eslint + a `lint` script)")
    if "prettier" not in dev and not any((fe / n).exists() for n in _FRONTEND_PRETTIER_FILES):
        findings.append("frontend: no Prettier configured (add prettier + a `format` script)")
    if not any(("tsc" in v or "typecheck" in k) for k, v in scripts.items()):
        findings.append("frontend: no typecheck script (add `tsc --noEmit`)")
    ts = fe / "tsconfig.json"
    if ts.exists():
        try:
            raw = ts.read_text(encoding="utf-8", errors="replace")
            if '"strict"' not in raw or re.search(r'"strict"\s*:\s*false', raw):
                findings.append("frontend: tsconfig not strict (set compilerOptions.strict=true)")
        except Exception:
            pass
    return findings


# ── Dependency lock (§2.3 / I7): every third-party import the engineer wrote must be a
# DECLARED dependency. Deterministic, advisory (folded into code_quality, non-blocking),
# scoped to the WRITTEN files so extend-mode never flags a big repo's pre-existing imports.
# Kills the "hallucinated react-query" drift class: code that imports a package nobody
# added to requirements.txt / package.json builds locally but breaks in a clean install.
# Biased toward PRECISION (few false positives) — a missed flag is fine, noise at the
# gate erodes trust. Never raises (registry contract). ───────────────────────────────

# Import-name → PyPI distribution-name aliases for the common cases where they differ, so
# `import yaml` declared as `PyYAML` is NOT falsely flagged. Everything else is covered by
# PEP503-normalised + boundary-prefix matching (sqlalchemy↔SQLAlchemy, psycopg2↔...-binary).
_PY_IMPORT_ALIASES = {
    "yaml": "pyyaml", "cv2": "opencv-python", "PIL": "pillow",
    "sklearn": "scikit-learn", "bs4": "beautifulsoup4", "jose": "python-jose",
    "jwt": "pyjwt", "dotenv": "python-dotenv", "dateutil": "python-dateutil",
    "multipart": "python-multipart", "psycopg2": "psycopg2-binary",
    "OpenSSL": "pyopenssl", "Crypto": "pycryptodome", "magic": "python-magic",
    "attr": "attrs", "slugify": "python-slugify", "dns": "dnspython",
    "jwt_extended": "flask-jwt-extended",
}

# Node built-in modules that need no package.json entry.
_NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "dns", "domain", "events", "fs", "http", "http2",
    "https", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads", "zlib",
}

# Skip dirs that hold deps/build output/VCS rather than first-party source.
_DEP_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env",
                  "dist", "build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _norm_dist(name: str) -> str:
    """PEP503-ish normalise a distribution/import name for comparison."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _is_test_path(p: Path) -> bool:
    """A test/e2e file — its imports (pytest fixtures, test-only libs) are noise here."""
    segs = {seg.lower() for seg in p.parts}
    return bool(segs & {"tests", "test", "e2e", "__tests__"}) or \
        p.name.startswith("test_") or p.name.endswith(("_test.py", ".spec.ts",
                                                       ".spec.tsx", ".test.ts", ".test.tsx"))


def _parse_requirements(text: str) -> set:
    """Pure: requirements.txt text → set of normalised distribution names. Handles version
    pins, extras, env markers and `pkg @ url`; ignores comments/options/blank. Does NOT
    follow `-r` includes (the file wrapper does)."""
    names = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        line = line.split(";", 1)[0].strip()            # drop env marker
        if " @ " in line:
            line = line.split(" @ ", 1)[0].strip()       # `pkg @ url`
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if m:
            names.add(_norm_dist(m.group(0)))
    return names


def _python_imports(source: str) -> set:
    """Pure: Python source → set of TOP-LEVEL absolute imported module names. Relative
    imports (`from . import x`) are first-party and skipped. Empty on a syntax error
    (the lint gate owns syntax)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:                               # relative → first-party
                continue
            if node.module:
                mods.add(node.module.split(".", 1)[0])
    return mods


def _js_imports(source: str) -> set:
    """Pure: JS/TS source → set of bare package specifiers (relative/path-alias/builtin
    excluded; scoped packages and sub-paths reduced to the installable package name)."""
    raw = set()
    for pat in (
        r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
        r"""require\(\s*['"]([^'"]+)['"]\s*\)""",
        r"""export\s+(?:\*|\{[^}]*\})\s+from\s+['"]([^'"]+)['"]""",
        r"""import\(\s*['"]([^'"]+)['"]\s*\)""",
    ):
        raw.update(re.findall(pat, source))
    specs = set()
    for spec in raw:
        if spec.startswith((".", "/", "@/", "~/", "node:")):
            continue
        if spec.startswith("@"):
            parts = spec.split("/")
            pkg = "/".join(parts[:2]) if len(parts) >= 2 else spec
        else:
            pkg = spec.split("/", 1)[0]
        if pkg in _NODE_BUILTINS:
            continue
        specs.add(pkg)
    return specs


def _package_json_deps(text: str) -> set:
    """Pure: package.json text → all declared dependency names (prod+dev+peer+optional)."""
    try:
        data = json.loads(text)
    except Exception:
        return set()
    out = set()
    for key in ("dependencies", "devDependencies", "peerDependencies",
                "optionalDependencies"):
        out.update((data.get(key) or {}).keys())
    return out


def _dep_satisfied(mod: str, declared: set) -> bool:
    """Is import `mod` covered by a declared (normalised) distribution name? Match on
    equality, alias, or a `-`-boundary prefix in either direction (so `psycopg2` ⊆
    `psycopg2-binary` and `google` ⊆ `google-cloud-storage`)."""
    cands = {_norm_dist(mod)}
    alias = _PY_IMPORT_ALIASES.get(mod) or _PY_IMPORT_ALIASES.get(mod.lower())
    if alias:
        cands.add(_norm_dist(alias))
    for c in cands:
        for d in declared:
            if d == c or d.startswith(c + "-") or c.startswith(d + "-"):
                return True
    return False


def _first_party_py_modules(root: Path) -> set:
    """Top-level names that are the project's OWN modules (packages with __init__.py and
    bare .py stems) — importing them is never an undeclared dependency. Over-includes by
    design (favours no-false-flags). Prunes dependency/build dirs so it stays cheap."""
    names = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _DEP_SKIP_DIRS]
        for d in dirnames:
            if (Path(dirpath) / d / "__init__.py").is_file():
                names.add(d)
        for f in filenames:
            if f.endswith(".py"):
                names.add(f[:-3])
    return names


def _read_python_deps(root: Path) -> tuple:
    """Collect declared Python deps from requirements*.txt (following `-r` includes) +
    pyproject.toml. Returns (declared_normalised_set, found_any_manifest)."""
    declared, seen = set(), set()
    found = False

    def _load_req(p: Path, depth=0):
        nonlocal found
        if depth > 3 or str(p) in seen or not p.is_file():
            return
        seen.add(str(p))
        found = True
        text = p.read_text(encoding="utf-8", errors="replace")
        declared.update(_parse_requirements(text))
        for raw in text.splitlines():
            m = re.match(r"(?:-r|--requirement)\s+(\S+)", raw.split("#", 1)[0].strip())
            if m:
                _load_req(p.parent / m.group(1), depth + 1)

    for req in [root / "requirements.txt", *sorted(root.glob("*/requirements.txt"))]:
        _load_req(req)
    for pyproj in [root / "pyproject.toml", *sorted(root.glob("*/pyproject.toml"))]:
        if not pyproj.is_file():
            continue
        found = True
        try:
            import tomllib                              # stdlib on 3.11+
            data = tomllib.loads(pyproj.read_text(encoding="utf-8", errors="replace"))
            for d in (data.get("project", {}).get("dependencies") or []):
                m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", d)
                if m:
                    declared.add(_norm_dist(m.group(0)))
            poetry = (data.get("tool", {}).get("poetry", {}).get("dependencies") or {})
            for name in poetry:
                if name.lower() != "python":
                    declared.add(_norm_dist(name))
        except Exception:
            pass
    return declared, found


def _check_python_deps(root: Path, files) -> list:
    py = [Path(f) for f in (files or [])
          if str(f).endswith(".py") and Path(f).is_file() and not _is_test_path(Path(f))]
    if not py:
        return []
    declared, found = _read_python_deps(root)
    if not found:
        return []                                       # no manifest yet — nothing to check against
    first_party = _first_party_py_modules(root)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    undeclared = set()
    for f in py:
        for mod in _python_imports(f.read_text(encoding="utf-8", errors="replace")):
            if not mod or mod.startswith("_") or mod in stdlib or mod in first_party:
                continue
            if not _dep_satisfied(mod, declared):
                undeclared.add(mod)
    if not undeclared:
        return []
    shown = ", ".join(sorted(undeclared)[:6])
    return [f"deps: {len(undeclared)} undeclared Python import(s) not in "
            f"requirements.txt/pyproject — {shown} (declare them or drop the import)"]


def _check_js_deps(root: Path, files) -> list:
    exts = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
    js = [Path(f) for f in (files or [])
          if str(f).endswith(exts) and Path(f).is_file() and not _is_test_path(Path(f))]
    if not js:
        return []
    pkg = next((c for c in [root / "package.json", *sorted(root.glob("*/package.json"))]
                if c.is_file()), None)
    if pkg is None:
        return []                                       # no manifest yet — nothing to check against
    declared = {_norm_dist(d) for d in
                _package_json_deps(pkg.read_text(encoding="utf-8", errors="replace"))}
    undeclared = set()
    for f in js:
        for spec in _js_imports(f.read_text(encoding="utf-8", errors="replace")):
            if _norm_dist(spec) not in declared:
                undeclared.add(spec)
    if not undeclared:
        return []
    shown = ", ".join(sorted(undeclared)[:6])
    return [f"deps: {len(undeclared)} undeclared JS/TS import(s) not in package.json — "
            f"{shown} (add to dependencies or drop the import)"]


def check_dependencies(project_dir: str, files=None) -> list:
    """ADVISORY (non-blocking) dependency-lock over the files the engineer just wrote:
    every third-party import must be a DECLARED dependency (requirements.txt/pyproject for
    Python, package.json for JS/TS). Folded into `code_quality` and surfaced at the PR gate
    like the security scan. Scoped to `files` so extend-mode never flags pre-existing repo
    imports. Returns human-readable strings; empty when clean / no manifest. Never raises."""
    root = Path(project_dir)
    findings: list = []
    try:
        findings += _check_python_deps(root, files)
    except Exception:
        pass
    try:
        findings += _check_js_deps(root, files)
    except Exception:
        pass
    return findings


# ── Code-quality SOFT GATE (§2.1/2.2) — OPT-IN, default OFF ──────────────────
# The complexity/type numbers are ADVISORY by default (code_quality_report, surfaced at the
# PR gate); coverage is measured + surfaced only when asked. Promoting any of them to a
# BLOCKING gate risks the engineer⇄QA loop (the cardinal rule: don't bolt on a gate that
# destabilizes it), so it is strictly OPT-IN behind QUALITY_GATE and follows the project's
# report-first → gate-later discipline:
#   unset / "off" → no change (complexity/type still advisory via code_quality_report)
#   "report"      → ALSO measure + surface line coverage (never blocks) — calibrate here first
#   "block"       → additionally FAIL the engineer round on over-budget COMPLEXITY
#                   (bounded by MAX_FIX_ATTEMPTS). The engineer can refactor to fix complexity.
# NOT gated even at "block": COVERAGE (the engineer is blocked from tests/ — it can't add
# tests to raise it; a coverage floor belongs on test_author, deferred) and mypy (its false
# positives without per-project config would cause loop-burn). Both stay report-only/advisory.
COVERAGE_FLOOR = 60          # %; surfaced as the floor; coverage is report-only (not gated here)
COMPLEXITY_BUDGET = 0        # functions allowed over cyclomatic _MAX_COMPLEXITY before blocking


def quality_gate_level() -> str:
    """'' (off) | 'report' | 'block', from QUALITY_GATE. Default '' = zero behavior change."""
    lvl = os.environ.get("QUALITY_GATE", "").strip().lower()
    return lvl if lvl in ("report", "block") else ""


def _complexity_over_budget(project_dir: str, files=None) -> int:
    """# functions exceeding cyclomatic complexity _MAX_COMPLEXITY in the written Python
    (deterministic, ruff C90). 0 when clean or ruff absent. Never raises."""
    py = [str(f) for f in (files or []) if str(f).endswith(".py") and Path(f).is_file()]
    targets = py or [project_dir]
    try:
        res = subprocess.run(
            ["ruff", "check", *targets, "--select=C90",
             f"--max-complexity={_MAX_COMPLEXITY}", "--statistics", "--quiet"],
            capture_output=True, text=True, timeout=120)
        return sum(c for c, code, _ in _parse_ruff_statistics(res.stdout) if code.startswith("C9"))
    except Exception:
        return 0


def check_quality_gate(project_dir: str, files=None) -> tuple[bool, str]:
    """OPT-IN soft gate (QUALITY_GATE=block): block the engineer round when code COMPLEXITY
    is over budget — the engineer CAN refactor to fix that. Returns (True, "") when the gate
    is off/report or clean (and never blocks on a missing tool). Coverage is NOT gated here
    (the engineer can't edit tests/ to raise it) and mypy stays advisory. Never raises."""
    if quality_gate_level() != "block":
        return True, ""
    over = _complexity_over_budget(project_dir, files)
    if over > COMPLEXITY_BUDGET:
        return False, (f"QUALITY GATE (opt-in QUALITY_GATE=block): {over} function(s) exceed "
                       f"cyclomatic complexity {_MAX_COMPLEXITY} (budget {COMPLEXITY_BUDGET}) — "
                       f"extract named helpers to flatten the control flow, then re-run.")
    return True, ""


# ── Kit testid uniqueness (responsive dual-layout hazard) ────────────────────
# The design skill MANDATES dual-surface layouts (desktop table → mobile cards), and a
# shared sub-component (e.g. a row-actions menu) rendered in BOTH layouts puts the SAME
# data-testid in the DOM twice — both present even though one is CSS-hidden. Playwright
# strict mode then fails ("resolved to 2 elements") on a bare get_by_test_id, and the
# engineer CANNOT fix it (the kit is design-owned). The testid-CONTRACT gate misses this
# (the id IS rendered — just twice). This catches it deterministically at kit-build time.
# Precise + low-false-positive: a component differentiated per usage (e.g. scope="-card")
# has DISTINCT usage strings and is NOT flagged — only IDENTICAL repeated usages are.


def _duplicate_testid_components(source: str) -> list:
    """Components in one kit file that render a data-testid AND are used 2+ times with
    IDENTICAL props — so their testid(s) appear multiple times in the rendered DOM. Pure."""
    defined = set(re.findall(r"function\s+([A-Z]\w+)\s*\(", source))
    defined |= set(re.findall(r"const\s+([A-Z]\w+)\s*[:=][^=]*=>", source))
    offenders = []
    for name in sorted(defined):
        m = re.search(rf"(?:function\s+{name}\s*\(|const\s+{name}\s*[:=])", source)
        if not m:
            continue
        body = source[m.end():]
        nxt = re.search(r"\n(?:function\s+[A-Z]|const\s+[A-Z]\w+\s*[:=][^=]*=>)", body)
        if nxt:
            body = body[: nxt.start()]
        if "data-testid" not in body:
            continue
        usages = re.findall(rf"<{name}\b[^>]*?/?>", source)
        norm = {}
        for u in usages:
            key = re.sub(r"\s+", " ", u).strip()
            norm[key] = norm.get(key, 0) + 1
        if any(v >= 2 for v in norm.values()):
            offenders.append(name)
    return offenders


def check_kit_testid_uniqueness(kit_dir) -> list:
    """Deterministic check over the design kit: flag data-testids that would appear MORE
    THAN ONCE in the DOM (responsive dual-layout dup). Returns human-readable findings;
    empty when clean. Never raises. Two cases: (1) a component rendering a data-testid used
    2+ times identically within a file; (2) a LITERAL data-testid string emitted in 2+
    distinct source sites across the kit."""
    findings: list = []
    try:
        kit_dir = Path(kit_dir)
        tsx = sorted(kit_dir.glob("*.tsx")) if kit_dir.is_dir() else []
        literal_sites: dict = {}
        for f in tsx:
            src = f.read_text(encoding="utf-8", errors="replace")
            for name in _duplicate_testid_components(src):
                findings.append(
                    f"{f.name}: <{name}> renders a data-testid and is used 2+ times "
                    f"IDENTICALLY — its testid(s) appear multiple times in the DOM "
                    f"(Playwright strict-mode hazard). Scope per layout (e.g. a suffix "
                    f"prop) or render one layout.")
            for lit in re.findall(r'data-testid=["\']([^"\'{}]+)["\']', src):
                literal_sites.setdefault(lit, []).append(f.name)
        for lit, files in sorted(literal_sites.items()):
            if len(files) >= 2:
                findings.append(
                    f'literal data-testid="{lit}" appears in {len(files)} source sites '
                    f'({", ".join(sorted(set(files)))}) — likely duplicated in the DOM; '
                    f"make each rendered testid unique.")
    except Exception:
        return findings
    return findings


# Phase 2.3: resource limits so a runaway/forkbomb in generated code can't take the host down.
DOCKER_LIMITS = ["--memory=512m", "--cpus=2", "--pids-limit=256"]


def _build_test_cmd(project_dir: str, test_path: str = "tests/", workdir: str = "") -> list:
    """Build the hardened `docker run` command for the Python/pytest phase."""
    install = "([ -f requirements.txt ] && pip install -r requirements.txt -q || true)"
    wd = "/app" + (f"/{workdir}" if workdir else "")
    return [
        "docker", "run", "--rm",
        *DOCKER_LIMITS,
        "-v", f"{project_dir}:/app",
        "-w", wd,
        PY_IMAGE,
        "bash", "-c",
        # --ignore=e2e: e2e specs are now pytest-playwright (Python) files — a
        # whole-suite run (extend mode, test_path="") must never collect them
        # into the unit-test container (no playwright there; they need the live stack).
        f"{install} && pytest {test_path} --ignore=e2e -x -q 2>&1",
    ]


def run_tests_in_docker(project_dir: str, timeout: int = 120, test_path: str = "tests/",
                        workdir: str = "") -> tuple[bool, str]:
    """
    Run pytest inside Docker, with resource limits. The Python-layer runner.
    Returns (passed, output_or_error).

    test_path: what to run — "tests/" for a greenfield project, or the existing repo's
    own target in extend mode (e.g. "" for the whole suite). If requirements.txt is
    absent (some repos use pyproject), the install step is skipped gracefully.
    workdir: subdir of the repo to run in (e.g. "backend" for a split-layout default stack).
    """
    cmd = _build_test_cmd(project_dir, test_path, workdir)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s."
    except FileNotFoundError:
        return False, "Docker not found. Is Docker running?"


# ── Toolchain detection + dispatch (the right test tool for the right stack) ──────
#
# The system's default stack is full-stack (FastAPI + Next.js + Postgres), so a single
# pytest-only runner could only ever test the Python layer. detect_toolchains() finds
# every testable layer by language marker (root + immediate subdirs, so both flat and
# backend/ + frontend/ layouts work), and run_project_tests() runs the matching tool for
# each and aggregates. Postgres-backed integration + Playwright e2e arrive in later slices.

_PY_MARKERS = ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg")


def _has_py_tests(d: Path) -> bool:
    if (d / "tests").is_dir():
        return True
    return any(d.glob("test_*.py")) or any(d.glob("*_test.py"))


def _node_runner(pkg_path: Path) -> str:
    """Pick the JS test runner from package.json — CONVENTION FIRST.

    If the project defines its own `test` script we run THAT (`npm test`): the project
    scopes its own unit run (e.g. it may exclude DB-dependent integration specs the
    throwaway container can't satisfy). Only when there is no `test` script do we fall
    back to invoking the raw tool (`vitest`/`jest`) by detected dependency — otherwise a
    naive `npx vitest run` sweeps in the whole suite, including tests that need a live
    database, and the engineer⇄QA loop burns every attempt on infra noise."""
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "npm-test"
    scripts = data.get("scripts", {})
    if isinstance(scripts, dict) and str(scripts.get("test", "")).strip():
        return "npm-test"
    deps = {**data.get("devDependencies", {}), **data.get("dependencies", {})}
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    return "npm-test"


def _detect_layer(d: Path, root: Path) -> dict | None:
    rel = "" if d == root else d.name
    if any((d / m).exists() for m in _PY_MARKERS) and _has_py_tests(d):
        return {"kind": "python", "dir": rel, "runner": "pytest"}
    pkg = d / "package.json"
    if pkg.exists():
        return {"kind": "node", "dir": rel, "runner": _node_runner(pkg)}
    return None


def detect_toolchains(project_dir: str) -> list[dict]:
    """Detect the test toolchains a project needs, one layer per language.

    Returns a list of {"kind", "dir", "runner"} — e.g.
    [{"kind":"python","dir":"backend","runner":"pytest"},
     {"kind":"node","dir":"frontend","runner":"vitest"}]. Empty list if nothing testable.
    Root markers win for a kind; immediate subdirs add kinds not already found at root.
    """
    root = Path(project_dir)
    if not root.is_dir():
        return []
    layers: list[dict] = []
    kinds_seen: set[str] = set()

    root_layer = _detect_layer(root, root)
    if root_layer:
        layers.append(root_layer)
        kinds_seen.add(root_layer["kind"])

    # e2e/ is the integration stage's domain (playwright specs + the npm droppings its
    # runner leaves behind) — it must never register as a unit-test layer: it sorts
    # before "frontend" and once stole the whole node slot, silently skipping vitest.
    for sub in sorted(p for p in root.iterdir() if p.is_dir()
                      and not p.name.startswith((".", "__"))
                      and p.name not in ("node_modules", "venv", "e2e")):
        layer = _detect_layer(sub, root)
        if layer and layer["kind"] not in kinds_seen:
            layers.append(layer)
            kinds_seen.add(layer["kind"])
    return layers


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """JS runners emit colored output even piped; the escape codes pollute error_log
    and waste LLM tokens — a live vitest failure was mostly \\x1b[39m noise."""
    return _ANSI_RE.sub("", text)


# A JS unit-test container has no database and only the platform's own npm binaries; when
# the output matches these patterns the failure is the ENVIRONMENT, not the code (mirrors
# the "environment, not code" port-conflict marker used by run_compose_integration).
_NODE_ENV_NOISE_RE = re.compile(
    r"Cannot find module '@rollup\/rollup-|Cannot find module .*native|"
    r"ECONNREFUSED.*5432|P1001|connect ECONNREFUSED"
)
_NODE_ENV_HINT = (
    "HINT: this looks like a TEST-ENVIRONMENT failure (missing platform binary / no "
    "database in the unit-test container), not necessarily a code bug — keep unit tests "
    "self-contained; DB-dependent tests belong in an integration suite run against a real "
    "database."
)


def _node_env_hint(output: str) -> str:
    """Pure: append the environment-not-code hint when the node output matches an
    infra-noise pattern (missing native/rollup binary, no database). Returns the output
    unchanged otherwise."""
    if output and _NODE_ENV_NOISE_RE.search(output):
        return f"{output}\n\n{_NODE_ENV_HINT}"
    return output


def _run_node_layer(project_dir: str, workdir: str, runner: str, timeout: int) -> tuple[bool, str]:
    """Run the JS test layer in a pinned node:alpine container."""
    cmd_map = {
        "vitest": "npx vitest run",
        "jest": "npx jest --ci",
        "npm-test": "npm test --silent",
    }
    run = cmd_map.get(runner, "npm test --silent")
    install = "(npm ci --silent || npm install --silent)"
    wd = "/app" + (f"/{workdir}" if workdir else "")
    cmd = [
        "docker", "run", "--rm", *DOCKER_LIMITS,
        "-v", f"{project_dir}:/app", "-w", wd,
        NODE_IMAGE, "sh", "-c", f"{install} && {run} 2>&1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = result.returncode == 0
        out = _strip_ansi((result.stdout + result.stderr).strip())
        return ok, out if ok else _node_env_hint(out)
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s."
    except FileNotFoundError:
        return False, "Docker not found. Is Docker running?"


def run_project_tests(project_dir: str, timeout: int = 300, test_path: str = None) -> tuple[bool, str]:
    """
    Detect every testable layer and run its matching tool, aggregating pass/fail.
    Returns (all_passed, combined_report). Replaces the pytest-only path for the
    engineer + QA so the default full-stack project is actually tested per layer.
    """
    layers = detect_toolchains(project_dir)
    if not layers:
        # Nothing auto-detected — fall back to legacy pytest behavior so flat Python
        # repos (and extend-mode repos) keep working.
        return run_tests_in_docker(project_dir, timeout=timeout,
                                   test_path=test_path if test_path is not None else "tests/")

    all_passed = True
    chunks = []
    for layer in layers:
        if layer["kind"] == "python":
            tp = test_path if test_path is not None else ("tests/" if not layer["dir"] else "tests/")
            ok, out = run_tests_in_docker(project_dir, timeout=timeout,
                                          test_path=tp, workdir=layer["dir"])
        elif layer["kind"] == "node":
            ok, out = _run_node_layer(project_dir, layer["dir"], layer["runner"], timeout)
        else:
            ok, out = True, "(no runner)"
        all_passed = all_passed and ok
        label = f"{layer['kind']} [{layer['runner']}] @ {layer['dir'] or '.'}"
        chunks.append(f"=== {label} — {'PASS' if ok else 'FAIL'} ===\n{out}")
    return all_passed, "\n\n".join(chunks)


# ── Coverage (§2.2) — report-only line coverage, SEPARATE from the correctness run ───
def _parse_coverage(output: str):
    """Pure: line-coverage % from a pytest-cov terminal report (the `TOTAL … NN%` row).
    None when no TOTAL row is present (e.g. pytest-cov absent / no tests collected)."""
    m = re.search(r"(?im)^TOTAL\s+\d+\s+\d+\s+(\d+)%", output or "")
    return int(m.group(1)) if m else None


def _build_coverage_cmd(project_dir: str, test_path: str = "tests/", workdir: str = "") -> list:
    """docker run for a SEPARATE best-effort coverage pass. Installs pytest-cov defensively
    and `|| true` everywhere so it can NEVER fail the build — it is purely a measurement."""
    install = ("([ -f requirements.txt ] && pip install -r requirements.txt -q || true) "
               "&& (pip install pytest-cov -q 2>/dev/null || true)")
    wd = "/app" + (f"/{workdir}" if workdir else "")
    return ["docker", "run", "--rm", *DOCKER_LIMITS, "-v", f"{project_dir}:/app", "-w", wd,
            PY_IMAGE, "bash", "-c",
            f"{install} && pytest {test_path} --ignore=e2e --cov=. --cov-report=term -q 2>&1 || true"]


def measure_coverage(project_dir: str, timeout: int = 240):
    """Best-effort line coverage % from a SEPARATE pytest-cov Docker run. It NEVER affects
    the correctness test run (own invocation) and NEVER raises. Targets the first detected
    Python layer (or the root). Returns None on any failure / no tooling / Docker absent —
    advisory by construction, so a missing number can never block or crash a run."""
    try:
        layers = [lyr for lyr in detect_toolchains(project_dir) if lyr["kind"] == "python"]
        workdir = layers[0]["dir"] if layers else ""
        res = subprocess.run(_build_coverage_cmd(project_dir, "tests/", workdir),
                             capture_output=True, text=True, timeout=timeout)
        return _parse_coverage(res.stdout + res.stderr)
    except Exception:
        return None


# ── Integration: bring the composed stack UP + smoke + e2e (Phase 4.2 / 4.3) ──────
#
# After QA passes, the integration stage proves the app actually RUNS: it brings the
# real stack up via the project's own docker-compose.yml, waits for healthy, smoke-checks
# the standard endpoints, and (4.3) runs QA's Playwright e2e specs against it.
# Conventions (enforced via skills/engineer.md): compose services named api / frontend /
# db; api publishes :8000 with GET /health; frontend publishes :3000.

PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright:v1.49.1-noble"  # pinned; legacy TS specs
PLAYWRIGHT_PY_IMAGE = "mcr.microsoft.com/playwright/python:v1.49.1-noble"  # pinned; e2e language = Python
PYTEST_PLAYWRIGHT_VERSION = "0.6.2"  # pinned plugin, matches playwright 1.49.x
COMPOSE_PROJECT = "graphsmith-it"   # fixed -p name → predictable network for e2e
_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

# Integration-only compose override. The e2e suite drives every rate-limited WRITE
# endpoint (signup/login/friend-request/follow) from ONE shared runner IP, so a per-IP
# limit that's correct for distinct real users 429s mid-suite — a recurring shared-IP
# TEST artifact (≥4th occurrence live), never a product bug. We relax it for the IT
# bring-up ONLY via a generated override; the SHIPPED compose keeps its production limit.
# Harmless for apps that don't read the var (an unused env value). Convention: api service.
IT_OVERRIDE_FILE = "docker-compose.it-override.yml"
_IT_OVERRIDE_YAML = (
    "# AUTO-GENERATED by the integration stage — relaxes per-IP rate limiting so the\n"
    "# shared-IP e2e suite cannot 429. NOT shipped (written + removed per IT run).\n"
    "services:\n"
    "  api:\n"
    "    environment:\n"
    '      RATE_LIMIT_ENABLED: "0"\n'
)


def has_compose_file(project_dir: str) -> bool:
    return any((Path(project_dir) / n).exists() for n in _COMPOSE_FILES)


def _compose_file_name(project_dir: str) -> str:
    """The project's base compose filename (so -f can name it explicitly — using -f
    disables compose's auto-discovery of the default file)."""
    return next((n for n in _COMPOSE_FILES if (Path(project_dir) / n).exists()),
                _COMPOSE_FILES[0])


def _write_it_override(project_dir: str) -> None:
    try:
        (Path(project_dir) / IT_OVERRIDE_FILE).write_text(_IT_OVERRIDE_YAML, encoding="utf-8")
    except OSError:
        pass   # best-effort — integration still runs, just with the shipped rate limit


def _remove_it_override(project_dir: str) -> None:
    try:
        (Path(project_dir) / IT_OVERRIDE_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def _compose(project_dir: str, *args: str, timeout: int = 120) -> tuple[int, str]:
    # Auto-include the integration override (rate-limit relaxation) when present so
    # up / health / logs / down all see the same merged config. The base file must be
    # named explicitly because passing -f disables compose's default-file discovery.
    files = []
    if (Path(project_dir) / IT_OVERRIDE_FILE).exists():
        files = ["-f", _compose_file_name(project_dir), "-f", IT_OVERRIDE_FILE]
    cmd = ["docker", "compose", "-p", COMPOSE_PROJECT, *files, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=project_dir)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, f"compose {' '.join(args)} timed out after {timeout}s"
    except FileNotFoundError:
        return 1, "Docker not found. Is Docker running?"


def _wait_healthy(project_dir: str, timeout: int = 120) -> tuple[bool, str]:
    """Poll compose ps until every container is running (and healthy, if healthchecked)."""
    import time
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        rc, out = _compose(project_dir, "ps", "--format", "json")
        if rc == 0 and out:
            rows = []
            for line in out.splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            if rows:
                bad = [r for r in rows
                       if r.get("State") != "running"
                       or (r.get("Health") and r["Health"] not in ("healthy", ""))]
                if not bad:
                    return True, "all services running/healthy"
                last = "; ".join(f"{r.get('Service', r.get('Name'))}={r.get('State')}/{r.get('Health', '')}"
                                 for r in bad)
        time.sleep(2)
    return False, f"services not healthy after {timeout}s: {last or 'no containers found'}"


# A service is treated as "the database" (last, hard-capped share of the log budget) when
# its name matches this — everything else is an APP service whose error we actually need.
_DB_SERVICE_RE = re.compile(r"(?i)(^|[-_])(db|database|postgres|postgresql|mysql|mariadb|"
                            r"mongo|mongodb|redis)([-_]|$)")


def _is_db_service(name: str) -> bool:
    return bool(_DB_SERVICE_RE.search(name or ""))


def _assemble_service_logs(per_service: dict, total_budget: int = 4000, db_cap: int = 15) -> str:
    """Pure: assemble per-service compose logs so ONE noisy service (usually the database's
    init chatter) can never evict the others. APP services print FIRST with the bulk of the
    character budget; DB services print LAST, hard-capped to `db_cap` lines each. `per_service`
    maps service name → its raw log text. A live integration failure was 100% db init noise —
    the real app-container error was invisible for 2 diagnosis attempts."""
    if not per_service:
        return "(no service logs captured)"
    app = {n: t for n, t in per_service.items() if not _is_db_service(n)}
    db = {n: t for n, t in per_service.items() if _is_db_service(n)}
    parts = []
    # App services get the char budget, split evenly and tail-sliced (the error is at the tail).
    share = max(400, total_budget // max(1, len(app))) if app else total_budget
    for name in sorted(app):
        parts.append(f"--- {name} (tail) ---\n{(app[name] or '').strip()[-share:]}")
    for name in sorted(db):
        tail = "\n".join((db[name] or "").strip().splitlines()[-db_cap:])
        parts.append(f"--- {name} (last {db_cap} lines) ---\n{tail}")
    return "\n\n".join(parts)


# When health never converges but the containers ARE running, the usual cause is a
# healthcheck probing `localhost` (IPv6 ::1) while the server binds IPv4 — the probe never
# passes though the app is fine. This hint mirrors the "environment, not code" markers.
_HEALTHCHECK_HINT = (
    "HINT: containers run but healthchecks never pass — verify the healthcheck probes "
    "127.0.0.1 (not localhost, which may resolve to IPv6 ::1 while the server binds IPv4) "
    "and allow a start_period.")


def _healthcheck_hint(health_msg: str) -> str:
    """Pure: return the healthcheck hint when the (failed) health message shows services
    that are RUNNING but with a non-healthy/starting probe state; else ''. The message
    format is `service=state/health` pairs from `_wait_healthy`."""
    msg = health_msg or ""
    # `_wait_healthy` emits `service=state/health` pairs; a container that is up but whose
    # probe hasn't passed shows `running/starting` (or `running/unhealthy`).
    running = "running/" in msg
    probe_failing = "starting" in msg or "unhealthy" in msg
    return _HEALTHCHECK_HINT if (running and probe_failing) else ""


def _capture_per_service_logs(project_dir: str) -> dict:
    """Fetch compose logs one service at a time so the assembler can budget them
    independently. Best-effort — a service with no logs maps to ''."""
    services: list = []
    rc, out = _compose(project_dir, "config", "--services")
    if rc == 0 and out:
        services = out.split()
    logs: dict = {}
    for svc in services:
        _rc, txt = _compose(project_dir, "logs", "--no-color", "--tail", "200", svc)
        logs[svc] = txt or ""
    if not logs:   # config --services failed — fall back to one blob under a synthetic name
        _rc, txt = _compose(project_dir, "logs", "--tail", "120")
        logs["all"] = txt or ""
    return logs


def _http_ok(url: str, timeout: int = 5) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _smoke(project_dir: str) -> tuple[bool, str]:
    """Hit the conventional endpoints for whichever standard services exist."""
    import time
    rc, out = _compose(project_dir, "config", "--services")
    services = set(out.split()) if rc == 0 else set()
    checks = []
    if "api" in services:
        checks.append(("api GET :8000/health", "http://localhost:8000/health"))
    if "frontend" in services:
        checks.append(("frontend GET :3000/", "http://localhost:3000/"))
    if not checks:
        return True, "no standard api/frontend services to smoke-check (compose health only)"
    results = []
    ok_all = True
    for label, url in checks:
        ok = False
        for _ in range(10):                       # service may be up but still warming
            if _http_ok(url):
                ok = True
                break
            time.sleep(2)
        ok_all = ok_all and ok
        results.append(f"{label}: {'OK' if ok else 'FAILED'}")
    return ok_all, "\n".join(results)


def _compose_network(project_dir: str) -> str:
    """Resolve the compose project's actual network name — apps may define a custom
    network (e.g. app_network → graphsmith-it_app_network), so never assume _default."""
    try:
        r = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"],
                           capture_output=True, text=True, timeout=30)
        for name in r.stdout.split():
            if name.startswith(f"{COMPOSE_PROJECT}_"):
                return name
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return f"{COMPOSE_PROJECT}_default"


def _run_e2e(project_dir: str, timeout: int = 600) -> tuple[bool, str]:
    """Run QA's Playwright specs against the composed stack.

    AUTHORING LANGUAGE IS PYTHON (CTO decision 2026-06-12): new specs are
    pytest-playwright files (e2e/test_*.py). Legacy TypeScript specs
    (e2e/*.spec.ts) keep running until ported. Both flavors run SERIALLY —
    spec files share ONE live backend DB; parallel workers cross-contaminate
    state (a live StatsBar count saw another spec's tasks the moment a second
    spec file appeared). e2e is correctness, not speed."""
    e2e_dir = Path(project_dir) / "e2e"
    py_specs = list(e2e_dir.glob("test_*.py"))
    ts_specs = list(e2e_dir.glob("*.spec.ts")) + list(e2e_dir.glob("*.spec.js"))
    if not py_specs and not ts_specs:
        return True, "no e2e specs found — skipped"
    ok, parts = True, []
    # tail-slice PER FLAVOR: a single tail over the combined output hid the python
    # section entirely (it runs first) when the report was sliced downstream
    if py_specs:
        py_ok, py_out = _run_e2e_python(project_dir, e2e_dir, timeout)
        ok = ok and py_ok
        parts.append(f"--- pytest-playwright (e2e/test_*.py) — "
                     f"{'OK' if py_ok else 'FAILED'} ---\n" + py_out[-2500:])
    if ts_specs:
        ts_ok, ts_out = _run_e2e_ts(project_dir, e2e_dir, timeout)
        ok = ok and ts_ok
        parts.append(f"--- legacy @playwright/test (e2e/*.spec.ts) — "
                     f"{'OK' if ts_ok else 'FAILED'} ---\n" + ts_out[-2500:])
    return ok, "\n\n".join(parts)


def _service_hosts(project_dir: str) -> tuple[str, str]:
    """The compose service names that publish :8000 (api) and :3000 (frontend),
    derived from the compose file itself. A live engineer named the api service
    'backend' and the hardcoded env override pointed the e2e specs at a hostname
    that didn't exist on the compose network (ENOTFOUND api) — never assume the
    conventional names, read the file."""
    api_host, fe_host = "api", "frontend"
    try:
        text = (Path(project_dir) / "docker-compose.yml").read_text(encoding="utf-8")
        for name, body in re.findall(r"^  ([\w-]+):\n((?:    .*\n|\s*\n)*)", text, re.M):
            if re.search(r"[\"']?8000:8000", body):
                api_host = name
            if re.search(r"[\"']?3000:3000", body):
                fe_host = name
    except OSError:
        pass
    return api_host, fe_host


def _e2e_docker_cmd(project_dir: str, e2e_dir: Path, image: str, script: str) -> list:
    # Specs are mounted READ-ONLY and copied to a container-local scratch dir: an
    # earlier run's npm install dropped package.json/node_modules INTO the project's
    # e2e/, which the toolchain detector then mistook for a unit-test node layer.
    api_host, fe_host = _service_hosts(project_dir)
    return [
        "docker", "run", "--rm", *DOCKER_LIMITS,
        "--network", _compose_network(project_dir),
        "-e", f"E2E_BASE_URL=http://{fe_host}:3000",
        "-e", f"API_BASE_URL=http://{api_host}:8000",
        "-v", f"{e2e_dir}:/specs:ro",
        image, "bash", "-c", script,
    ]


def _run_e2e_python(project_dir: str, e2e_dir: Path, timeout: int) -> tuple[bool, str]:
    # playwright must be pinned WITH the plugin: an unpinned install resolved
    # playwright 1.60 against the image's 1.49 browsers → BrowserType.launch
    # "Executable doesn't exist" (live failure).
    pw_version = PLAYWRIGHT_PY_IMAGE.rsplit(":v", 1)[1].split("-")[0]
    script = ("mkdir -p /scratch && cp /specs/test_*.py /scratch/ && cd /scratch && "
              f"pip install -q pytest-playwright=={PYTEST_PLAYWRIGHT_VERSION} "
              f"playwright=={pw_version} 1>/dev/null 2>&1; "
              "pytest -q -p no:cacheprovider --browser chromium --tb=short 2>&1")
    cmd = _e2e_docker_cmd(project_dir, e2e_dir, PLAYWRIGHT_PY_IMAGE, script)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"pytest-playwright run timed out after {timeout}s"
    except FileNotFoundError:
        return False, "Docker not found. Is Docker running?"


def _run_e2e_ts(project_dir: str, e2e_dir: Path, timeout: int) -> tuple[bool, str]:
    pw_version = PLAYWRIGHT_IMAGE.rsplit(":v", 1)[1].split("-")[0]
    script = ("mkdir -p /scratch && cp /specs/*.spec.* /scratch/ && cd /scratch && "
              "npm init -y >/dev/null 2>&1; "
              f"npm i -D @playwright/test@{pw_version} --silent >/dev/null 2>&1 "
              "&& npx playwright test --workers=1 --reporter=line 2>&1")
    cmd = _e2e_docker_cmd(project_dir, e2e_dir, PLAYWRIGHT_IMAGE, script)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"playwright run timed out after {timeout}s"
    except FileNotFoundError:
        return False, "Docker not found. Is Docker running?"


_KIT_SKIP_DIRS = {"node_modules", ".next", ".git", "__pycache__", "dist", "build"}


# ── Interface Contract (additive-freeze across phases) ───────────────────────

def _kit_component_names(text: str) -> set:
    """Exported component names declared in a kit source — used to find their JSX usages
    elsewhere in the kit. Empty → the caller falls back to the file's stem."""
    names = set(re.findall(r'export\s+(?:default\s+)?function\s+(\w+)', text))
    names |= set(re.findall(r'export\s+const\s+(\w+)\s*[:=]', text))
    return names


def _kit_suffixers(sources: dict) -> dict:
    """Kit components that re-emit their OWN `data-testid` prop with a template-literal
    SUFFIX (`data-testid={`${testId}-add-friend`}`). (Live phase-2 TrailTribe class:
    RelationshipButton renders `${testId}-add-friend` / `-requested` / `-accept` /
    `-friends` / `-edit`; an e2e that asserted the bare base matched no element yet the
    string-grep gates passed because the base IS in the source as a prop literal.)
    Returns {ComponentName: (suffixes:set[leading-dash], default_base:str|None, verbatim)}
    where `verbatim` is True for a HYBRID that ALSO forwards the base unchanged
    (`data-testid={testId}`, e.g. UsernameField → the real input PLUS sub-state spans) —
    for those the bare base DOES render and must NOT be suppressed. A component that ONLY
    forwards verbatim (FollowButton) has no suffix and is deliberately NOT included."""
    out = {}
    for name, text in sources.items():
        m = re.search(r"""["']data-testid["']\s*:\s*(\w+)\s*(?:=\s*["']([\w-]+)["'])?""",
                      text)
        if not m:
            continue
        alias, default_base = m.group(1), m.group(2)
        suffixes = set(re.findall(
            r'data-testid=\{`\$\{' + re.escape(alias) + r'\}(-[\w-]+)`', text))
        if not suffixes:
            continue
        verbatim = bool(re.search(r'data-testid=\{' + re.escape(alias) + r'\}', text)
                        or re.search(r'data-testid=\{`\$\{' + re.escape(alias) + r'\}`\}',
                                     text))
        for comp in (_kit_component_names(text) or {Path(name).stem}):
            out[comp] = (suffixes, default_base, verbatim)
    return out


def kit_state_suffixes(sources: dict) -> dict:
    """{ComponentName: sorted([suffix,…])} for state-suffix renderers — surfaced to QA's
    e2e authoring so it builds `<base><suffix>` selectors, not the never-rendered base."""
    return {c: sorted(s) for c, (s, _b, _v) in _kit_suffixers(sources).items()}


def resolve_kit_testids(sources: dict) -> tuple:
    """(static_testids, dynamic_prefixes) ACTUALLY rendered in the DOM, resolving the
    state-suffix components above. `sources` maps a label (filename) → TSX source.

    The naive `data-testid="…"` grep can't tell a real rendered element from a base PROP
    handed to a suffix-renderer: `<RelationshipButton data-testid="profile-relationship"/>`
    renders `profile-relationship-add-friend`, never the bare `profile-relationship`. So
    for every usage of a suffix-renderer we emit `<base>-<suffix>` and DROP the bare base
    before the generic scan — UNLESS the component is a hybrid that also forwards the base
    verbatim (then the bare base renders too and is kept). A pure verbatim forwarder's base
    is left as-is by the generic scan (it renders as-is)."""
    suffixers = _kit_suffixers(sources)
    static, prefixes = set(), set()
    # a suffix-renderer with a default base renders these when used bare — emitting them
    # pins the SUFFIX SET so a later rework can't silently rename/drop a state variant.
    for _comp, (suffixes, default_base, verbatim) in suffixers.items():
        if default_base:
            static |= {default_base + s for s in suffixes}
            if verbatim:
                static.add(default_base)
    for _name, text in sources.items():
        work = text
        for comp, (suffixes, _default, verbatim) in suffixers.items():
            def _resolve(mm, _suf=suffixes, _verb=verbatim):
                el = mm.group(0)
                lit = re.search(r'data-testid=["\']([\w-]+)["\']', el)
                if lit:                                    # static base → concrete ids
                    static.update(lit.group(1) + s for s in _suf)
                    if _verb:                              # hybrid: bare base ALSO renders
                        static.add(lit.group(1))
                tpl = re.search(r'data-testid=\{`([\w-]*)\$\{', el)
                if tpl and tpl.group(1):                   # dynamic base → literal prefix
                    prefixes.add(tpl.group(1))
                # drop the consumed base prop so the generic scan below won't read it as a
                # rendered id; for a hybrid we re-add the base explicitly above.
                return re.sub(
                    r'\s*data-testid=(?:["\'][\w-]+["\']|\{`[^`]*`\}|\{[\w.]+\})', '', el)
            work = re.sub(r'<' + re.escape(comp) + r'\b(?:[^>]|=>)*?/?>', _resolve, work)
        static |= set(re.findall(r'data-testid=["\']([\w-]+)["\']', work))
        static |= set(re.findall(r'testId=["\']([\w-]+)["\']', work))
        static |= set(re.findall(r'testid=["\']([\w-]+)["\']', work))
        prefixes |= set(re.findall(r'data-testid=\{`([\w-]+?)\$\{', work))
        prefixes |= set(re.findall(r'testid=\{`([\w-]+?)\$\{', work))
    return static, prefixes


def extract_kit_interface(kit_dir, manifest_text: str = "") -> tuple:
    """The kit's PUBLIC interface: (static_testids, dynamic_prefixes, microcopy).
    testids/prefixes come from the kit source (ground truth, with suffix-renderers
    resolved — see resolve_kit_testids); required microcopy from the manifest's REQUIRED
    MICROCOPY section. This is what e2e specs depend on, so it must only GROW across
    phases — never drop an entry a prior-phase spec relies on."""
    kit_dir = Path(kit_dir)
    sources = {}
    if kit_dir.is_dir():
        for p in list(kit_dir.glob("*.tsx")) + list(kit_dir.glob("*.ts")):
            sources[p.name] = p.read_text(encoding="utf-8", errors="replace")
    static, prefixes = resolve_kit_testids(sources)
    micro = set()
    m = re.search(r"REQUIRED MICROCOPY.*?\n(.*?)(?:\n#|\Z)", manifest_text or "",
                  re.DOTALL | re.I)
    if m:
        micro = set(re.findall(r'-\s*"(.+?)"', m.group(1)))
    return static, prefixes, micro


def _render_interface_contract(testids: set, prefixes: set, micro: set) -> str:
    return ("# Interface Contract (additive-only — the kit GUARANTEES these across phases)\n\n"
            "## TESTIDS\n" + "\n".join(f"- {t}" for t in sorted(testids))
            + ("\n\n## TESTID PREFIXES\n" + "\n".join(f"- {p}" for p in sorted(prefixes))
               if prefixes else "")
            + "\n\n## REQUIRED MICROCOPY\n" + "\n".join(f'- "{s}"' for s in sorted(micro)) + "\n")


def parse_interface_contract(text: str) -> tuple:
    """(testids, prefixes, microcopy) from a persisted interface_contract.md."""
    def _sec(name, pat):
        m = re.search(rf"##\s*{name}\s*\n(.*?)(?:\n##\s|\Z)", text or "", re.DOTALL | re.I)
        return set(re.findall(pat, m.group(1))) if m else set()
    return (_sec("TESTIDS", r"-\s*([\w-]+)"),
            _sec("TESTID PREFIXES", r"-\s*([\w-]+)"),
            _sec("REQUIRED MICROCOPY", r'-\s*"(.+?)"'))


MAX_PRODUCT_INVARIANTS_CHARS = 2500


def _find_dir(root: Path, *candidates):
    for c in candidates:
        d = root / c
        if d.is_dir():
            return d
    return None


def extract_product_invariants(project_root, cap: int = MAX_PRODUCT_INVARIANTS_CHARS) -> str:
    """STATIC, code-verifiable product context for the generation agents (architect /
    test_author / engineer / qa), parsed OFF DISK from a FastAPI+SQLAlchemy backend's
    models + routers — NEVER the runtime openapi.json (which only exists when compose is
    healthy, so it would silently go stale on the common compose-fail path).

    Returns "" when no models dir is found (non-Python or undetected repo) so callers skip
    cleanly — mirroring how profile/ledger are managed-project-only. Output is a compact,
    capped markdown digest of the load-bearing invariants a new feature can silently
    violate: unique/check constraints, computed-not-stored columns, enums, and the
    route+auth surface. Every line is reconcilable against the real models/routers."""
    if not project_root:
        return ""
    root = Path(project_root)
    if not root.is_dir():
        return ""
    models_dir = _find_dir(root, "backend/app/models", "app/models", "backend/models", "models")
    routers_dir = _find_dir(root, "backend/app/routers", "app/routers", "backend/routers", "routers")
    if models_dir is None:
        return ""

    out = []
    model_lines = []
    for p in sorted(models_dir.glob("*.py")):
        if p.name == "__init__.py":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"class\s+(\w+)\([^)]*Base[^)]*\):", text)
        cls = m.group(1) if m else p.stem
        facts = []
        for u in re.findall(r"UniqueConstraint\(([^)]*)\)", text):
            cols = [c for c in re.findall(r'"([^"]+)"', u) if not c.startswith(("uq_", "ix_", "ck_"))]
            if cols:
                facts.append(f"UNIQUE({', '.join(cols)})")
        # Column-level unique=True (handles multi-line mapped_column(...) defs by
        # scanning back to the nearest column name before each unique=True).
        for mu in re.finditer(r"unique=True", text):
            names = re.findall(r"(\w+):\s*Mapped\[", text[:mu.start()])
            if names:
                facts.append(f"UNIQUE({names[-1]})")
        for c in re.findall(r'CheckConstraint\(\s*["\']([^"\']+)["\']', text):
            facts.append(f"CHECK({c})")
        for line in text.splitlines():
            if re.search(r"\b(never stored|computed|derived)\b", line, re.I):
                clean = " ".join(line.replace('"', " ").split()).strip("# -")
                if 10 < len(clean) < 150:
                    facts.append("note: " + clean)
                    break
        for em in re.finditer(r"class\s+(\w+)\(str,\s*enum\.Enum\):(.*?)(?=\nclass |\Z)", text, re.DOTALL):
            vals = re.findall(r'=\s*"([^"]+)"', em.group(2))
            if vals:
                facts.append(f"enum {em.group(1)}: {', '.join(vals)}")
        if facts:
            seen = set()
            uniq = [f for f in facts if not (f in seen or seen.add(f))]
            model_lines.append(f"- **{cls}**: " + "; ".join(uniq))
    if model_lines:
        out.append("### Model invariants (statically verifiable against the backend models)")
        out.extend(model_lines)

    if routers_dir is not None:
        route_lines = []
        for p in sorted(routers_dir.glob("*.py")):
            if p.name == "__init__.py":
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            mp = re.search(r"APIRouter\((.*?)\)", text, re.DOTALL)
            blk = mp.group(1) if mp else ""
            mpf = re.search(r'prefix=["\']([^"\']+)', blk)
            prefix = mpf.group(1) if mpf else ""
            guards = sorted(set(re.findall(r"Depends\((get_current_\w+)\)", text)))
            methods = re.findall(r"@\w+\.(?:get|post|put|patch|delete)\(", text)
            auth = "PUBLIC" if not guards else ", ".join(g.replace("get_current_", "") for g in guards)
            if methods:
                route_lines.append(f"- {p.stem} ({prefix or '/'}): {len(methods)} routes — auth: {auth}")
        if route_lines:
            out.append("\n### Route/auth surface (verify against the backend routers)")
            out.extend(route_lines)

    result = "\n".join(out).strip()
    if not result:
        return ""
    if len(result) > cap:
        result = result[:cap].rstrip() + "\n…(truncated — full detail in docs/DOMAIN_MODEL.md + product/api_contract.md)"
    return result


def check_interface_additive(prior_text: str, cur_testids: set, cur_prefixes: set,
                             cur_micro: set) -> tuple:
    """Compare the current kit interface to the persisted contract. Returns
    (ok, dropped_message, merged_contract_text). A DROP of any prior testid/prefix/
    microcopy is a regression (prior-phase e2e specs depend on it) → not ok."""
    p_ids, p_prefixes, p_micro = parse_interface_contract(prior_text)
    dropped = []
    for t in sorted(p_ids - cur_testids):
        # A prior BARE base (e.g. 'profile-relationship') is NOT a regression once the
        # current kit renders its state-suffixed children ('profile-relationship-add-friend'
        # …): resolve_kit_testids stopped emitting the never-rendered base, but the
        # guarantee lives on in the suffixed ids the e2e specs actually use. (Phase-2
        # suffix-renderer fix left this base in the persisted contract — don't false-flag.)
        if any(c.startswith(t + "-") for c in cur_testids):
            continue
        dropped.append(f"data-testid '{t}'")
    for t in sorted(p_prefixes - cur_prefixes):
        dropped.append(f"testid prefix '{t}'")
    for s in sorted(p_micro - cur_micro):
        dropped.append(f'microcopy "{s}"')
    merged = _render_interface_contract(p_ids | cur_testids, p_prefixes | cur_prefixes,
                                        p_micro | cur_micro)
    if dropped:
        msg = ("INTERFACE REGRESSION — the kit no longer provides these prior-phase "
               "guarantees (existing e2e specs depend on them; restore them):\n"
               + "\n".join(f"- {d}" for d in dropped))
        return False, msg, merged
    return True, f"interface additive ok ({len(cur_testids)} testids, {len(cur_micro)} microcopy)", merged


def check_kit_wiring(project_dir: str, kit_files: list) -> tuple[bool, str]:
    """I3 — kit-wiring ENFORCEMENT (protection ≠ usage). The engineer is blocked from
    EDITING the design kit, but nothing forced it to USE it: a live run built parallel
    components and shipped 17 missing microcopy strings past every prompt rule.
    Deterministic, post-write, like the linter:
      1) at least one non-kit frontend source must import from the kit dir;
      2) no non-kit file may duplicate a kit component's basename (parallel component).
    Returns (ok, message-with-exact-rules-for-the-engineer)."""
    if not kit_files:
        return True, "no design kit — nothing to enforce"
    root = Path(project_dir)
    kit_names = {Path(k).name for k in kit_files}
    kit_paths = {(root / k).resolve() for k in kit_files}

    _kit_import_re = re.compile(r"""from\s+['"][^'"]*\bkit(?:/|['"])""")
    wired, dupes, containers, sources_seen = False, [], [], 0
    for f in root.rglob("*"):
        if f.suffix not in (".tsx", ".ts", ".jsx", ".js"):
            continue
        if any(part in _KIT_SKIP_DIRS or part in ("tests", "e2e") for part in f.parts):
            continue
        if f.resolve() in kit_paths or "kit" in f.parent.parts[-1:]:
            continue
        sources_seen += 1
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        # match BOTH `from '.../kit/X'` (deep import) AND `from '.../kit'` (barrel
        # import via the kit's index.ts — idiomatic and clean; a live engineer used it
        # and the deep-only regex wrongly failed it for 2 rounds).
        imports_kit = bool(_kit_import_re.search(text))
        if imports_kit:
            wired = True
        if f.name in kit_names:
            # A same-named file that IMPORTS from the kit is a legitimate CONTAINER/wrapper
            # (it composes the kit component, not a parallel reimplementation) — allow it.
            # A live run stalled 3 rounds because a valid `components/AuthSheet.tsx` wrapping
            # the kit's AuthSheet was flagged as a duplicate.
            if imports_kit:
                containers.append(str(f.relative_to(root)))
            else:
                dupes.append(str(f.relative_to(root)))

    findings = []
    if not wired and sources_seen:
        findings.append(
            "the design kit is NOT WIRED: no page/container imports from the kit "
            "directory. Import the kit components (e.g. `import { X } from "
            "'./kit/X'`) and pass data/handlers via their props — do NOT rebuild "
            "their UI yourself.")
    for d in dupes:
        findings.append(
            f"PARALLEL COMPONENT: {d} duplicates a design-kit component's name WITHOUT "
            f"importing the kit — it reimplements a component the kit owns. Fix EITHER by: "
            f"rename the file (e.g. <Name>Controller / <Name>Container) OR make it import "
            f"and wrap the kit component — do not reimplement it.")
    if findings:
        return False, "\n".join(f"- {f}" for f in findings)
    note = "kit wired; no parallel components"
    if containers:
        note += f" ({len(containers)} kit-named container(s) that import the kit — allowed)"
    return True, note


def check_testid_contract(project_dir: str) -> tuple[bool, str]:
    """FREE deterministic gate (phase-3 live lesson): every data-testid asserted by the
    EXISTING e2e specs must be rendered somewhere in the frontend source. A feature
    rework that drops prior phases' testids silently breaks the whole regression
    suite at the most expensive verification stage — catch it BEFORE compose/e2e."""
    root = Path(project_dir)
    specs = root / "e2e"
    if not specs.is_dir():
        return True, "no e2e specs — skipped"
    asserted = set()
    for f in list(specs.glob("test_*.py")) + list(specs.glob("*.spec.ts")):
        t = f.read_text(encoding="utf-8", errors="replace")
        asserted |= set(re.findall(r"get_by_test_id\(\s*f?['\"]([\w-]+)", t))
        asserted |= set(re.findall(r"getByTestId\(\s*['\"`]([\w-]+)", t))
    src = root / "frontend" / "src"
    files = list(src.rglob("*.tsx")) + list(src.rglob("*.ts")) if src.is_dir() else []
    if not files:   # flat layouts
        files = [p for p in root.rglob("*.tsx") if "node_modules" not in p.parts]
    sources = {str(f): f.read_text(encoding="utf-8", errors="replace") for f in files
               if "node_modules" not in f.parts and ".next" not in f.parts}
    # resolve_kit_testids returns the REAL rendered ids: a base PROP handed to a
    # suffix-renderer (`<RelationshipButton data-testid="profile-relationship"/>`) is NOT
    # a rendered element — only `profile-relationship-<state>` is. The old flat grep
    # counted the prop literal as "rendered" and passed a base-only assertion (live).
    static, prefixes = resolve_kit_testids(sources)
    rendered = static | prefixes
    missing = sorted(a for a in asserted
                     if a not in rendered
                     and not any(a.startswith(p) and len(a) > len(p) for p in rendered))
    if missing:
        return False, ("TESTID CONTRACT BROKEN — the e2e suite asserts testids the UI no "
                       "longer renders (a rework dropped prior-phase elements):\n"
                       + "\n".join(f"- {m}" for m in missing))
    return True, f"all {len(asserted)} asserted testids are rendered"


def lint_e2e_spec(content: str, kit_testids: set = None, testid_prefixes: tuple = (),
                  known_paths_text: str = "") -> list:
    """Deterministic quality gate for QA-authored Playwright specs (I4).
    Returns findings (empty = clean). Every rule traces to a LIVE failure:
    invented testids, getByLabel unions matching the form's aria-label instead of
    the input (twice in one run), a nonexistent `.overdue` class locator, /tasks
    vs /api/tasks paths (twice), .check() silently no-opping on styled checkboxes,
    and cross-test data pollution from missing isolation."""
    findings = []
    kit = bool(kit_testids or testid_prefixes)
    python = bool(re.search(r"^\s*def test_", content, re.MULTILINE)) or "import pytest" in content
    # Lint CODE, not commentary: specs legitimately cite the conventions in comments
    # (a live first-pass spec was flagged for "// .check() can silently no-op").
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    content = re.sub(r"^\s*//.*$|\s//[^'\"`\n]*$", "", content, flags=re.MULTILINE)
    content = re.sub(r"^\s*#.*$", "", content, flags=re.MULTILINE)

    for tid in re.findall(r"get(?:ByTestId|_by_test_id)\(\s*[frb]?['\"`]([^'\"`]+)['\"`]\s*\)",
                          content):
        tid = tid.split("{")[0]   # f-string/template dynamic suffix → prefix check
        if kit and tid not in (kit_testids or set()) \
                and not any(tid.startswith(p) for p in testid_prefixes):
            findings.append(f"test id '{tid}': no such data-testid in the design kit")

    if kit:
        for m in re.findall(r"get(?:ByLabel|_by_label|ByPlaceholder|_by_placeholder)\([^)]*\)",
                            content):
            findings.append(f"{m}: label/placeholder guessing — use the kit's data-testids "
                            f"(a live getByLabel(/task/i) matched the FORM's aria-label)")
        for m in re.findall(r"locator\(\s*[frb]?['\"`]\s*\.[\w-]+\s*['\"`]\s*\)", content):
            findings.append(f"{m}: bare CSS-class locator — class names are not a contract; "
                            f"use data-testids")

    if re.search(r"(?:request|api)\w*\.(?:post|put|patch)\([^)]*\bjson\s*=", content):
        findings.append("Playwright sync request.post/put/patch takes data=, NOT json= — "
                        "use data={...} (a live spec failed with 'unexpected keyword json')")
    if re.search(r"\.(?:check|uncheck)\(\s*\)", content):
        findings.append(".check()/.uncheck(): flaky on styled checkboxes — use "
                        "locator.evaluate('el => el.click()')")

    if known_paths_text:
        known = set(re.findall(r"['\"`](/[\w\-][\w/\-]*)", known_paths_text))
        used = set(re.findall(r"['\"`](/api/[\w/\-${}.:]*)", content))
        used |= set(re.findall(r"\$\{API\}(/[\w/\-${}.:]*)", content))   # TS template
        used |= set(re.findall(r"\{API\}(/[\w/\-{}.:]*)", content))      # py f-string
        for p in used:
            base = re.split(r"\$?\{", p)[0].rstrip("/")
            if base and base not in known \
                    and not any(base.startswith(k + "/") for k in known):
                findings.append(f"API path {p}: not found in the implementation or tech "
                                f"spec — copy paths exactly, never guess")

    isolated = ("beforeEach" in content) or \
               (python and re.search(r"@pytest\.fixture\(.*autouse=True", content))
    if ".fill(" in content and not isolated:
        findings.append("spec creates data but has no cleanup (test.beforeEach / autouse "
                        "fixture) — shared live DB means leaked entities pollute other "
                        "specs' counts")

    # belt-and-suspenders: a stray markdown fence anywhere is a guaranteed SyntaxError
    if re.search(r"^```", content, re.MULTILINE):
        findings.append("markdown code fence inside the spec file — emit raw TypeScript only")
    return findings


def _playwright_screenshot(url: str, out_png: str, network: str = None,
                           mount: tuple = None, timeout: int = 240,
                           full_page: bool = False) -> tuple[bool, str]:
    """Screenshot a URL with the pinned Playwright image. Used by the design-QA
    stage: the live app (on the compose network) and the mockup (file:// mount)."""
    out_dir = str(Path(out_png).resolve().parent)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm", *DOCKER_LIMITS]
    if network:
        cmd += ["--network", network]
    if mount:
        cmd += ["-v", f"{mount[0]}:{mount[1]}:ro"]
    fp = "--full-page " if full_page else ""
    cmd += ["-v", f"{out_dir}:/shots",
            PLAYWRIGHT_IMAGE, "sh", "-c",
            f"npx -y playwright@1.49.1 screenshot --viewport-size=1280,900 {fp}"
            f"--wait-for-timeout=4000 '{url}' /shots/{Path(out_png).name} 2>&1"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0 and Path(out_png).exists()
        return ok, (r.stdout + r.stderr).strip()[-500:]
    except subprocess.TimeoutExpired:
        return False, f"screenshot timed out after {timeout}s"
    except FileNotFoundError:
        return False, "Docker not found. Is Docker running?"


def capture_app_screenshot(project_dir: str, out_png: str) -> tuple[bool, str]:
    """Screenshot the RUNNING composed app's frontend (call while the stack is up)."""
    return _playwright_screenshot("http://frontend:3000", out_png,
                                  network=_compose_network(project_dir))


def render_mockup_screenshot(mockup_path: str, out_png: str) -> tuple[bool, str]:
    """Render the design mockup HTML to a PNG (needs network for the Tailwind CDN).
    Full-page: design boards are taller than a viewport — a 900px crop once hid the
    populated-state screen and the vision judge invented details it couldn't see."""
    src = Path(mockup_path).resolve()
    return _playwright_screenshot(f"file:///work/{src.name}", out_png,
                                  mount=(str(src.parent), "/work"), full_page=True)


def check_required_microcopy(required: list, url: str = "http://localhost:3000/") -> tuple[bool, str]:
    """FREE design-conformance gate: every REQUIRED MICROCOPY string from the design
    manifest must appear verbatim in the served app — the SSR HTML *or* the shipped JS
    bundles. Conditional-state copy (validation errors, toasts, busy labels) never
    renders in initial SSR but DOES ship in the chunks, so the corpus is page + its
    same-origin scripts. Deterministic — no LLM, no screenshots."""
    import html as html_mod
    import re as re_mod
    import urllib.request
    from urllib.parse import urljoin

    def fetch(u):
        with urllib.request.urlopen(u, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")

    try:
        page = fetch(url)
    except Exception as e:
        return False, f"could not fetch frontend HTML for microcopy check: {e}"

    # Route-split apps put feature copy in PER-ROUTE chunks: crawl one level of
    # same-origin links (login/signup/any public page) and include their chunks too.
    # (Live: ALL phase-2 strings lived on /discover — invisible from "/" alone.)
    pages = [html_mod.unescape(page)]
    hrefs = {h for h in re_mod.findall(r'href="(/[^"#?]*)"', page) if not h.startswith("//")}
    for href in sorted(hrefs)[:8]:
        try:
            pages.append(html_mod.unescape(fetch(urljoin(url, href))))
        except Exception:
            continue

    corpus = list(pages)
    seen_src = set()
    all_srcs = []
    for pg in pages:
        for src in re_mod.findall(r'<script[^>]+src="([^"]+)"', pg)[:20]:
            if src not in seen_src:
                seen_src.add(src)
                all_srcs.append(src)
    for src in all_srcs:
        try:
            chunk = fetch(urljoin(url, src))
            # JS string literals escape unicode (e.g. ’ for ’) — unescape both ways
            corpus.append(chunk)
            corpus.append(chunk.encode().decode("unicode_escape", errors="ignore"))
        except Exception:
            continue
    blob = "\n".join(corpus)

    missing = [s for s in required if s not in blob]
    if not missing:
        return True, f"all {len(required)} required microcopy strings present"
    # Auth-gated, code-split routes are UNREACHABLE for this unauthenticated free
    # gate — report honestly and let the logged-in e2e flows + vision design-QA
    # carry verification for those strings instead of failing the run on a blind spot.
    auth_walled = any(p in blob for p in ("/login", "/signup", "Log in", "Sign up"))
    if auth_walled and len(missing) < len(required):
        return True, (f"{len(required) - len(missing)}/{len(required)} strings verified on "
                      f"public pages; {len(missing)} NOT VERIFIABLE without auth "
                      f"(code-split authed routes) — covered by e2e + design-QA instead:\n"
                      + "\n".join(f'- "{m}"' for m in missing))
    return False, ("DESIGN MICROCOPY MISSING from the running app (the design-owned "
                   "copy must appear verbatim in the page or its shipped bundles):\n" +
                   "\n".join(f'- "{s}"' for s in missing))


def _seo_findings(page_html: str) -> list:
    """Deterministic SEO/AEO floor for a consumer app's served HTML (SSR output).
    Pure function — testable without a server. Returns human-actionable misses."""
    h = page_html.lower()
    findings = []
    if not re.search(r"<title[^>]*>[^<]+</title>", h):
        findings.append("missing <title>")
    if not re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'][^"\']+', h) and \
       not re.search(r'<meta[^>]+content=["\'][^"\']+["\'][^>]+name=["\']description["\']', h):
        findings.append('missing <meta name="description">')
    if not re.search(r"<h1[\s>]", h):
        findings.append("missing <h1> (exactly one per page)")
    if not re.search(r"<html[^>]+lang=", h):
        findings.append('missing lang attribute on <html>')
    if not re.search(r'<meta[^>]+name=["\']viewport["\']', h):
        findings.append('missing viewport meta')
    if 'application/ld+json' not in h:
        findings.append("missing JSON-LD structured data (schema.org — AI/answer-engine "
                        "optimization needs machine-readable page semantics)")
    return findings


def _theme_findings(page_html: str) -> list:
    """Deterministic dual-theme floor: the served HTML must carry the theme toggle and
    dark-mode variant classes (Tailwind class names are visible in SSR output)."""
    findings = []
    if "theme-toggle" not in page_html:
        findings.append('missing ThemeToggle (data-testid="theme-toggle") in the app chrome')
    if "dark:" not in page_html:
        findings.append("no dark: variant classes in the served HTML — dark mode not implemented")
    return findings


def check_theme_floor(url: str = "http://localhost:3000/") -> tuple[bool, str]:
    """FREE dual-theme gate (no LLM): light+dark mode is a design mandate — the served
    page must ship the toggle and dark-variant styling."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"could not fetch frontend HTML for theme check: {e}"
    findings = _theme_findings(page)
    if findings:
        return False, "DUAL-THEME MANDATE MISSING:\n" + "\n".join(f"- {f}" for f in findings)
    return True, "theme floor present (theme-toggle + dark: variants)"


def check_seo_basics(url: str = "http://localhost:3000/") -> tuple[bool, str]:
    """FREE SEO/AEO gate (no LLM): the served frontend must carry the discoverability
    floor — title, meta description, h1, lang, viewport, JSON-LD. Server-rendered HTML
    is what crawlers and AI answer engines read; client-only content doesn't count."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"could not fetch frontend HTML for SEO check: {e}"
    findings = _seo_findings(page)
    if findings:
        return False, "SEO/AEO BASICS MISSING from the served HTML:\n" + \
                      "\n".join(f"- {f}" for f in findings)
    return True, "SEO/AEO floor present (title, description, h1, lang, viewport, JSON-LD)"


def _foreign_port_holders(ports=(8000, 3000)) -> str:
    """Anything still LISTENING on the app's ports after our own stack is down is a
    FOREIGN process (a stale debug stack burned a live integration attempt — and the
    engineer was handed the bind error as if it were a code bug). Best-effort lsof;
    empty string = all clear."""
    lines = []
    for port in ports:
        try:
            r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=10)
            body = (r.stdout or "").strip().splitlines()
            if len(body) > 1:   # header + at least one holder
                lines.append(f":{port} → " + "; ".join(body[1:3]))
        except Exception:
            continue
    return "\n".join(lines)


def _payload_from_schema(schema: dict, defs: dict, variant: int = 0) -> dict:
    """Synthesize a minimal valid JSON body from an openapi schema: required string
    fields get labeled sample text; date fields vary by variant so the seeded set
    covers OVERDUE (yesterday), today-ish, and future states."""
    import datetime
    if "$ref" in schema:
        schema = defs.get(schema["$ref"].rsplit("/", 1)[-1], {})
    payload = {}
    today = datetime.date.today()
    dates = [today - datetime.timedelta(days=1),    # overdue — the state that matters
             today + datetime.timedelta(days=1),
             today + datetime.timedelta(days=7)]
    labels = ["Seeded task (overdue)", "Seeded task (due soon)", "Seeded task (later)"]
    props = schema.get("properties") or {}
    required = schema.get("required") or list(props)[:2]
    for name, p in props.items():
        if "$ref" in p:
            p = defs.get(p["$ref"].rsplit("/", 1)[-1], {})
        t = p.get("type")
        if t is None and "anyOf" in p:   # Optional[...] in FastAPI
            inner = [x for x in p["anyOf"] if x.get("type") not in (None, "null")]
            p = inner[0] if inner else {}
            t = p.get("type")
        if name not in required and p.get("format") not in ("date", "date-time"):
            continue   # keep payloads minimal, but always include date fields (states!)
        if t == "string" and p.get("format") == "date":
            payload[name] = dates[variant % 3].isoformat()
        elif t == "string" and p.get("format") == "date-time":
            payload[name] = dates[variant % 3].isoformat() + "T12:00:00Z"
        elif t == "string":
            payload[name] = labels[variant % 3]
        elif t == "boolean":
            payload[name] = False
        elif t in ("integer", "number"):
            payload[name] = 1
        elif t == "array":
            payload[name] = []
    return payload


def seed_app_data(api: str = "http://localhost:8000", n: int = 3) -> tuple[bool, str]:
    """I6b: put representative data into the live app BEFORE the design-QA screenshot.
    An empty-state screenshot made visual verification meaningless in a live run — the
    populated states (incl. an OVERDUE entity) are what the chosen design must show.
    Discovery is openapi-driven (FastAPI serves /openapi.json), fully deterministic,
    best-effort: any failure skips seeding rather than failing integration."""
    import json as json_mod
    import time
    import urllib.request
    try:
        spec = None
        for attempt in range(4):   # "healthy" containers can precede a LISTENING uvicorn
            try:
                with urllib.request.urlopen(f"{api}/openapi.json", timeout=8) as resp:
                    spec = json_mod.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2)
        defs = (spec.get("components") or {}).get("schemas") or {}
        candidates = sorted(
            (path, op) for path, ops in (spec.get("paths") or {}).items()
            for verb, op in ops.items()
            if verb == "post" and "auth" not in path and "login" not in path
               and (op.get("requestBody") or {}))
        if not candidates:
            return False, "no POST collection endpoint in openapi.json — seeding skipped"
        path, op = candidates[0]   # shortest path = the main collection
        schema = (op["requestBody"].get("content") or {}).get(
            "application/json", {}).get("schema") or {}
        posted = 0
        for i in range(n):
            body = json_mod.dumps(_payload_from_schema(schema, defs, i)).encode()
            req = urllib.request.Request(f"{api}{path}", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                if 200 <= resp.status < 300:
                    posted += 1
        return posted > 0, f"seeded {posted} entities via POST {path}"
    except Exception as e:
        return False, f"seeding skipped ({type(e).__name__}: {e})"


def run_compose_integration(project_dir: str, require_compose: bool = True,
                            e2e: bool = True, timeout: int = 900,
                            screenshot_to: str = None,
                            required_microcopy: list = None,
                            check_seo: bool = False) -> tuple[bool, str]:
    """
    Bring the app's own compose stack up, verify it runs, run e2e, tear it down.
    Returns (passed, report). Never raises; compose is always torn down.
    require_compose=False (external --repo) turns a missing compose file into a
    graceful skip instead of a failure.
    """
    if not has_compose_file(project_dir):
        if require_compose:
            return False, ("INTEGRATION FAILURE: no docker-compose.yml at the project root. "
                           "The default stack must ship its own compose file (services: api, "
                           "frontend, db) so the app can be brought up and verified.")
        return True, "no compose file — integration skipped (external repo)"

    # I6a: pre-clean — tear down any leftover stack of OURS, then fail fast (clearly
    # marked as an ENVIRONMENT problem) if a foreign process still holds the ports,
    # instead of burning an engineer round on a bind error that no code change fixes.
    # Drop any override left by a crashed prior run so the pre-clean targets the base.
    _remove_it_override(project_dir)
    _compose(project_dir, "down", "-v", "--remove-orphans", timeout=120)
    busy = _foreign_port_holders()
    if busy:
        return False, ("INTEGRATION BLOCKED (environment, not code): the app's ports are "
                       "held by processes outside this project's compose stack:\n" + busy +
                       "\nFree the ports and re-run. Do NOT change application code for this.")

    # Relax per-IP rate limiting for the shared-IP e2e suite (IT-only; _compose picks it
    # up from here on). The shipped compose is untouched — production keeps its limit.
    _write_it_override(project_dir)
    report = []
    passed = False
    try:
        rc, out = _compose(project_dir, "up", "-d", "--build", timeout=timeout)
        # On a build failure the REAL cause (a TS/compile error) sits in the MIDDLE of a
        # long build log — a plain tail slice loses it and the agents fix blind (the
        # never-head-slice-a-test-log rule, now for build logs). Keep the error lines.
        build_out = out[-2000:]
        if rc != 0:
            err_lines = [l for l in _strip_ansi(out).splitlines()
                         if re.search(r"error TS|Module not found|Cannot find|Failed to "
                                      r"compile|is not assignable|has no exported|does not "
                                      r"exist|ERROR|manifest unknown|\.tsx?\(\d+", l, re.I)]
            if err_lines:
                build_out = "COMPILE/BUILD ERRORS:\n" + "\n".join(err_lines[-25:]) + \
                            "\n\n(tail)\n" + out[-1200:]
        report.append(f"=== compose up --build — {'OK' if rc == 0 else 'FAILED'} ===\n{build_out}")
        if rc != 0:
            return False, "\n\n".join(report)

        ok, msg = _wait_healthy(project_dir)
        if not ok:
            hint = _healthcheck_hint(msg)
            if hint:
                msg = f"{msg}\n{hint}"
        report.append(f"=== health — {'OK' if ok else 'FAILED'} ===\n{msg}")
        if ok:
            ok, msg = _smoke(project_dir)
            report.append(f"=== smoke — {'OK' if ok else 'FAILED'} ===\n{msg}")
        if ok and required_microcopy:
            ok, msg = check_required_microcopy(required_microcopy)
            report.append(f"=== design microcopy (deterministic) — {'OK' if ok else 'FAILED'} ===\n{msg}")
        if ok and check_seo:
            ok, msg = check_seo_basics()
            report.append(f"=== SEO/AEO floor (deterministic) — {'OK' if ok else 'FAILED'} ===\n{msg}")
        if ok and check_seo:   # same UI-feature flag: dual-theme is a design mandate
            ok, msg = check_theme_floor()
            report.append(f"=== dual-theme floor (deterministic) — {'OK' if ok else 'FAILED'} ===\n{msg}")
        if ok and e2e:
            ok, msg = check_testid_contract(project_dir)
            report.append(f"=== testid contract (deterministic) — {'OK' if ok else 'FAILED'} ===\n{msg}")
        if ok and e2e:
            ok, msg = _run_e2e(project_dir)
            report.append(f"=== e2e (playwright) — {'OK' if ok else 'FAILED'} ===\n{msg[-6000:]}")
        # Capture the app screenshot for the design-QA stage WHILE the stack is still
        # up — design_qa runs after teardown and must not re-compose the whole stack.
        # I6b: seed representative data first (incl. an OVERDUE entity) — an empty-state
        # screenshot made the visual verification meaningless in a live run.
        if ok and screenshot_to:
            _seeded, seed_msg = seed_app_data()
            report.append(f"=== screenshot seed data — {'OK' if _seeded else 'SKIPPED'} ===\n{seed_msg}")
            shot_ok, shot_msg = capture_app_screenshot(project_dir, screenshot_to)
            report.append(f"=== app screenshot — {'OK' if shot_ok else 'FAILED'} ===\n"
                          f"{screenshot_to if shot_ok else shot_msg}")
        passed = ok
        if not passed:
            # Per-service so the db's init noise can't evict the app container's real error.
            logs = _assemble_service_logs(_capture_per_service_logs(project_dir))
            report.append(f"=== service logs (per service) ===\n{logs}")
        return passed, "\n\n".join(report)
    finally:
        _compose(project_dir, "down", "-v", "--remove-orphans", timeout=120)
        _remove_it_override(project_dir)   # never ship the IT-only override
