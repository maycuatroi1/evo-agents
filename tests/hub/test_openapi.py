"""``evo-agents hub openapi`` prints the API contract the web client is generated from: the same document the app
serves at /v1/openapi.json, without Postgres, GitHub or any EVO_HUB_* variable, byte for byte the same on every run."""

import json

import pytest

pytest.importorskip("fastapi")

from evo_agents.hub.openapi import PLACEHOLDER_DSN, document, render
from tests.hub import pg

SHELL_ROUTES = {"/v1/auth/whoami", "/v1/projects", "/v1/projects/{project}", "/v1/admin/stats", "/v1/auth/web/csrf"}


def openapi(*args: str, **env: str):
    return pg.cli(["hub", "openapi", *args], pg.clean_env(**env))


def test_prints_the_document_the_app_serves_without_any_hub_variable():
    result = openapi()
    assert result.returncode == 0, result.stderr
    spec = json.loads(result.stdout)
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "evo-agents hub"
    assert SHELL_ROUTES <= set(spec["paths"])
    assert spec == document()
    assert PLACEHOLDER_DSN not in result.stdout


def test_output_is_stable_and_ignores_the_environment(tmp_path):
    first = openapi()
    second = openapi(
        EVO_HUB_DSN="postgresql://someone:hunter2@db.invalid/hub",
        EVO_HUB_PUBLIC_URL="https://hub.example.org",
        EVO_HUB_ADMINS="octo",
    )
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert "hunter2" not in second.stdout + second.stderr


def test_output_flag_writes_the_file_and_keeps_stdout_empty(tmp_path):
    target = tmp_path / "openapi.json"
    result = openapi("--output", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert target.read_text(encoding="utf-8") == render(document())
    lines = pg.log_lines(result.stderr)
    assert [line["msg"] for line in lines] == ["openapi document written"]


def test_errors_are_json_bodies_in_the_contract():
    spec = document()
    schemas = spec["components"]["schemas"]
    assert {"error", "message"} <= set(schemas["ErrorBody"]["properties"])
    forbidden = spec["paths"]["/v1/admin/stats"]["get"]["responses"]["403"]
    assert forbidden["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/ErrorBody"
