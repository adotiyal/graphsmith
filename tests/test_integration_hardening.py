"""
Pre-e2e hardening (I6a/I6b + critic note) — every item traces to a live failure:
a stale stack on :8000 burned an integration attempt (and the engineer was handed
the bind error as a "code bug"); an empty-state screenshot made design verification
meaningless; the critic doesn't know the 3-direction spec format.
"""

import pytest

from tools import registry


def test_compose_precleans_before_up(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    calls = []

    def fake_compose(project_dir, *args, timeout=None):
        calls.append(args[0])
        if args[0] == "up":
            return 1, "boom"        # stop the run right after up
        return 0, ""

    monkeypatch.setattr(registry, "_compose", fake_compose)
    monkeypatch.setattr(registry, "_foreign_port_holders", lambda ports=(8000, 3000): "")
    ok, report = registry.run_compose_integration(str(tmp_path), e2e=False)
    assert calls[0] == "down"                      # pre-clean BEFORE up
    assert "up" in calls
    assert not ok


def test_foreign_port_holder_fails_fast_as_environment(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setattr(registry, "_compose", lambda *a, **k: (0, ""))
    monkeypatch.setattr(registry, "_foreign_port_holders",
                        lambda ports=(8000, 3000): ":8000 → node 12345 (some-other-app)")
    ok, report = registry.run_compose_integration(str(tmp_path), e2e=False)
    assert not ok
    assert "environment, not code" in report       # never blames the engineer
    assert "Do NOT change application code" in report
    assert "12345" in report


OPENAPI = {
    "paths": {
        "/api/tasks": {
            "post": {"requestBody": {"content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/TaskCreate"}}}}}},
        "/api/auth/login": {"post": {"requestBody": {"content": {}}}},
    },
    "components": {"schemas": {"TaskCreate": {
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string"},
            "due_date": {"anyOf": [{"type": "string", "format": "date"},
                                   {"type": "null"}]},
            "completed": {"type": "boolean"},
        }}}},
}


def test_payload_from_schema_covers_overdue_state():
    import datetime
    defs = OPENAPI["components"]["schemas"]
    schema = {"$ref": "#/components/schemas/TaskCreate"}
    p0 = registry._payload_from_schema(schema, defs, variant=0)
    assert "Seeded" in p0["title"]
    assert p0["due_date"] == (datetime.date.today()
                              - datetime.timedelta(days=1)).isoformat()  # OVERDUE
    p1 = registry._payload_from_schema(schema, defs, variant=1)
    assert p1["due_date"] > datetime.date.today().isoformat()            # future


def test_seed_app_data_posts_to_main_collection(monkeypatch):
    import io, json
    posted = []

    class FakeResp:
        status = 201
        def __init__(self, body=b"{}"):
            self._b = body
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/openapi.json"):
            return FakeResp(json.dumps(OPENAPI).encode())
        posted.append((url, json.loads(req.data.decode())))
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ok, msg = registry.seed_app_data()
    assert ok and "POST /api/tasks" in msg
    assert len(posted) == 3
    assert all(u.endswith("/api/tasks") for u, _ in posted)   # auth path skipped
    assert any("overdue" in body["title"] for _, body in posted)


def test_seed_app_data_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    ok, msg = registry.seed_app_data()
    assert not ok and "skipped" in msg


def test_service_hosts_derived_from_compose(tmp_path):
    # Live failure: services named backend/frontend (not api) → the hardcoded
    # API_BASE_URL=http://api:8000 was ENOTFOUND on the compose network.
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  db:\n    image: postgres\n"
        "  backend:\n    ports:\n      - \"8000:8000\"\n"
        "  frontend:\n    ports:\n      - \"3000:3000\"\n")
    assert registry._service_hosts(str(tmp_path)) == ("backend", "frontend")
    # no compose file → conventional defaults
    assert registry._service_hosts(str(tmp_path / "nope")) == ("api", "frontend")
    cmd = registry._e2e_docker_cmd(str(tmp_path), tmp_path, "img", "true")
    assert "API_BASE_URL=http://backend:8000" in cmd
    assert "E2E_BASE_URL=http://frontend:3000" in cmd


def test_critic_design_focus_knows_direction_sections():
    from agents.critic import STAGE_CONFIG
    focus = STAGE_CONFIG["design"]["review_focus"]
    assert "Design Directions" in focus and "Chosen Direction" in focus
    assert "unchosen directions" in focus  # critic must not flag the two unchosen directions as gaps


def test_testid_contract_gate(tmp_path):
    # Phase-3 live lesson: a UI rework dropped prior-phase testids and silently
    # broke the entire e2e suite at the most expensive stage.
    (tmp_path / "e2e").mkdir()
    (tmp_path / "e2e" / "test_x.py").write_text(
        'page.get_by_test_id("profile-bio")\npage.get_by_test_id(f"mutual-friend-{uid}")\n')
    src = tmp_path / "frontend" / "src"
    src.mkdir(parents=True)
    (src / "View.tsx").write_text('<p data-testid="profile-bio">x</p>')
    ok, msg = registry.check_testid_contract(str(tmp_path))
    assert not ok and "mutual-friend-" in msg and "profile-bio" not in msg
    # render the missing one (as a dynamic prefix) -> green
    (src / "M.tsx").write_text('<li data-testid={`mutual-friend-${id}`} />')
    ok, msg = registry.check_testid_contract(str(tmp_path))
    assert ok, msg
