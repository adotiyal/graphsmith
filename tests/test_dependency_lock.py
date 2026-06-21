"""
Dependency-lock check (§2.3 / I7) — every third-party import the engineer writes must be a
DECLARED dependency (requirements.txt/pyproject for Python, package.json for JS/TS).

Mirrors the code-quality layer's test discipline: the pure parsers/matchers are tested
deterministically; `check_dependencies` is tested end to end on a tmp project tree (it must
flag the undeclared, never the declared/stdlib/first-party/relative/aliased, degrade
gracefully with no manifest, and never raise); the engineer surfacing mirrors
test_hardening.py::test_engineer_surfaces_security_warnings.
"""

from pathlib import Path

from conftest import base_state, seed
from tools.registry import (
    check_dependencies,
    _parse_requirements, _python_imports, _js_imports, _package_json_deps,
    _dep_satisfied, _norm_dist, _is_test_path,
)


# ── pure parsers / matchers (deterministic, no filesystem) ───────────────────

def test_parse_requirements_handles_pins_extras_markers_urls():
    out = _parse_requirements(
        "fastapi==0.110.0\n"
        "uvicorn[standard]>=0.27\n"
        "PyYAML==6.0  ; python_version >= '3.8'\n"
        "# a comment\n"
        "\n"
        "-r base.txt\n"                                   # include — NOT followed by the pure fn
        "requests @ https://example.com/r.tar.gz\n"
        "SQLAlchemy\n"
    )
    assert out == {"fastapi", "uvicorn", "pyyaml", "requests", "sqlalchemy"}


def test_python_imports_top_level_only_and_skips_relative():
    src = (
        "import os, sys\n"
        "import requests\n"
        "from fastapi import FastAPI\n"
        "from . import models\n"               # relative → first-party
        "from .routers import users\n"         # relative → first-party
        "import app.config\n"                  # → top-level 'app'
    )
    assert _python_imports(src) == {"os", "sys", "requests", "fastapi", "app"}


def test_python_imports_empty_on_syntax_error():
    assert _python_imports("import (this is not valid python") == set()


def test_js_imports_reduces_to_package_and_skips_relative_alias_builtin():
    src = (
        "import React from 'react';\n"
        "import { useState } from \"react\";\n"
        "import api from './api';\n"                       # relative
        "import { Button } from '@/components/ui';\n"      # path alias
        "import xx from '@scope/pkg/sub';\n"               # scoped + sub-path
        "const y = require('lodash');\n"
        "export { z } from 'date-fns';\n"
        "const m = await import('chart.js');\n"
        "import 'normalize.css';\n"
        "import fs from 'node:fs';\n"                       # builtin
    )
    assert _js_imports(src) == {"react", "@scope/pkg", "lodash",
                                "date-fns", "chart.js", "normalize.css"}


def test_package_json_deps_unions_all_dep_buckets():
    text = ('{"dependencies":{"react":"^18"},'
            '"devDependencies":{"vitest":"^1"},'
            '"peerDependencies":{"next":"^15"}}')
    assert _package_json_deps(text) == {"react", "vitest", "next"}


def test_dep_satisfied_alias_and_boundary_prefix():
    assert _dep_satisfied("yaml", {"pyyaml"})                       # alias
    assert _dep_satisfied("psycopg2", {"psycopg2-binary"})          # boundary prefix
    assert _dep_satisfied("sqlalchemy", {"sqlalchemy"})             # case-normalised
    assert _dep_satisfied("google", {"google-cloud-storage"})       # namespace prefix
    assert not _dep_satisfied("foobar", {"fastapi", "requests"})    # genuinely missing


def test_norm_dist_and_is_test_path():
    assert _norm_dist("SQL_Alchemy.Core") == "sql-alchemy-core"
    assert _is_test_path(Path("tests/test_x.py"))
    assert _is_test_path(Path("backend/tests/test_y.py"))
    assert _is_test_path(Path("e2e/flow.spec.ts"))
    assert not _is_test_path(Path("src/main.py"))


# ── check_dependencies end-to-end (tmp project tree) ─────────────────────────

def _write(p: Path, content: str) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_flags_undeclared_python_import(tmp_path):
    _write(tmp_path / "requirements.txt", "fastapi==0.110.0\n")
    f = _write(tmp_path / "src" / "main.py", "import fastapi\nimport requests\n")
    out = check_dependencies(str(tmp_path), [f])
    assert len(out) == 1
    assert "deps:" in out[0] and "requests" in out[0] and "fastapi" not in out[0]


def test_clean_when_all_python_imports_accounted_for(tmp_path):
    _write(tmp_path / "requirements.txt", "fastapi\n")
    _write(tmp_path / "src" / "mypkg.py", "X = 1\n")                 # first-party module
    f = _write(tmp_path / "src" / "main.py",
               "import os\nimport json\nfrom fastapi import X\n"
               "from . import other\nimport mypkg\n")
    # os/json stdlib · fastapi declared · relative skipped · mypkg first-party
    assert check_dependencies(str(tmp_path), [f]) == []


def test_no_findings_without_a_manifest(tmp_path):
    f = _write(tmp_path / "src" / "main.py", "import requests\n")   # no requirements/pyproject
    assert check_dependencies(str(tmp_path), [f]) == []


def test_aliased_packages_not_flagged(tmp_path):
    _write(tmp_path / "requirements.txt", "PyYAML\npsycopg2-binary\n")
    f = _write(tmp_path / "db.py", "import yaml\nimport psycopg2\n")
    assert check_dependencies(str(tmp_path), [f]) == []


def test_pyproject_dependencies_are_read(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "fastapi"]\n')
    f = _write(tmp_path / "app.py", "import httpx\nimport fastapi\nimport boto3\n")
    out = check_dependencies(str(tmp_path), [f])
    assert len(out) == 1 and "boto3" in out[0]                      # only the undeclared one


def test_flags_undeclared_js_import(tmp_path):
    _write(tmp_path / "package.json", '{"dependencies":{"react":"^18"}}')
    f = _write(tmp_path / "src" / "App.tsx",
               "import React from 'react'\nimport { z } from 'date-fns'\nimport './x'\n")
    out = check_dependencies(str(tmp_path), [f])
    assert len(out) == 1 and "date-fns" in out[0] and "react" not in out[0]


def test_split_layout_frontend_package_json(tmp_path):
    _write(tmp_path / "frontend" / "package.json", '{"dependencies":{"next":"^15"}}')
    f = _write(tmp_path / "frontend" / "page.tsx",
               "import Link from 'next/link'\nimport axios from 'axios'\n")
    out = check_dependencies(str(tmp_path), [f])
    assert len(out) == 1 and "axios" in out[0]                      # next/link is declared


def test_test_files_are_ignored(tmp_path):
    _write(tmp_path / "requirements.txt", "fastapi\n")
    f = _write(tmp_path / "tests" / "test_x.py", "import pytest\nimport requests\n")
    assert check_dependencies(str(tmp_path), [f]) == []             # test-only deps are noise


def test_never_raises_and_empty_on_unresolvable(tmp_path):
    assert check_dependencies("/no/such/dir", ["/nope.py"]) == []
    assert check_dependencies(str(tmp_path), None) == []


# ── engineer surfacing (mirrors test_engineer_surfaces_security_warnings) ────

def test_engineer_folds_dependency_findings_into_code_quality(llm, ws, no_docker, monkeypatch):
    from agents import engineer
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /run")
    seed(ws, "proj", "tests", "test_run.py", "def test_run():\n    assert True")
    # no_docker stubs check_dependencies to []; re-patch it to a finding to prove the
    # engineer folds dependency findings into the surfaced `code_quality` list.
    monkeypatch.setattr(
        engineer, "check_dependencies",
        lambda d, files=None: ["deps: 1 undeclared Python import(s) not in "
                               "requirements.txt/pyproject — requests (declare them or drop)"])
    llm.default = "===FILE: src/main.py===\nimport requests\n===END==="
    out = engineer.run(base_state(design_path=tech))
    assert any("undeclared" in c and "deps:" in c for c in out["code_quality"])
