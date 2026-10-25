"""The catalog of model providers (``evo_agents.hub.model_catalog``, ``scripts/generate_model_catalog.py``): the file
is OmniRoute's v3.8.52, the version of the gateway image, and holds every chat LLM reached by a base URL and a key
and none of the OAuth, cookie, no-auth, search, audio, image, video, cloud-agent or system providers; the generator's
filter is checked on rows without Node; a member reads the catalog by GET /v1/model-catalog and by
``evo-agents hub model-catalog``.

The data and the generator's filter run without Postgres; the route and the command skip without EVO_HUB_TEST_DSN."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from evo_agents.hub import model_catalog
from evo_agents.hub.contract import commands
from tests.hub import pg
from tests.hub import test_secrets_cli as secrets_cli
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok
from tests.hub.test_secrets_cli import needs_pg, run_cli

ROOT = Path(__file__).resolve().parents[2]
FORMATS = {"openai", "anthropic", "openai-responses", "gemini"}
KEYS = {
    "id", "alias", "name", "group", "color", "text_icon", "website", "key_hint", "base_url", "api_format",
    "default_model", "key_required", "free", "via",
}  # fmt: skip


def generator():
    spec = importlib.util.spec_from_file_location("generate_model_catalog", ROOT / "scripts/generate_model_catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(id: str, family: str = "frontier", sets=(), registry=None, **display) -> dict:
    shown = {"name": id.title(), "serviceKinds": ["llm"], "website": f"https://{id}.test", **display}
    entry = {
        "format": "openai",
        "authType": "apikey",
        "executor": "default",
        "baseUrl": f"https://{id}.test/v1/chat/completions",
    }
    entry.update(registry or {})
    return {"family": family, "id": id, "display": shown, "registry": entry, "sets": list(sets)}


# The file


def test_model_catalog_is_of_the_version_of_the_gateway_image():
    source = model_catalog.load()["source"]
    assert source["version"] == model_catalog.GATEWAY_VERSION, (
        "the catalog and the gateway image must be of one OmniRoute version: regenerate with "
        f"`python scripts/generate_model_catalog.py v{model_catalog.GATEWAY_VERSION}`, or move GATEWAY_VERSION"
    )
    assert source["tag"] == f"v{source['version']}" and source["name"] == "OmniRoute"
    assert re.fullmatch(r"[0-9a-f]{40}", source["commit"]) and source["commit"].startswith("ad6f33f6")
    assert source["license"] == "MIT" and source["copyright"].startswith("Copyright")
    assert source["repository"] == "https://github.com/cheaper-inference/OmniRoute"


def test_model_catalog_entries_are_whole_and_unique():
    catalog = model_catalog.load()
    entries = model_catalog.providers()
    assert len(entries) > 150
    assert len({e["id"] for e in entries}) == len(entries)
    gateway = [e for e in entries if e["via"] == "gateway" and not e["id"].startswith("custom-")]
    assert len({e["alias"] for e in gateway}) == len(gateway), "the gateway reads the alias in a model name"
    for e in entries:
        assert set(e) == KEYS, e["id"]
        assert e["group"] in catalog["groups"] and e["api_format"] in FORMATS, e["id"]
        assert e["name"] and e["alias"] and e["color"].startswith("#") and e["text_icon"], e["id"]
        assert not re.search(r"/(chat/completions|messages|responses)(\?|$)", e["base_url"]), e["id"]
        assert "·" not in str(e), e["id"]
    for e in gateway:
        assert re.match(r"https?://", e["base_url"]), e["id"]
        assert e["key_hint"], e["id"]


def test_model_catalog_holds_the_providers_a_key_reaches_and_no_other():
    by_id = {e["id"]: e for e in model_catalog.providers()}
    assert {"openai", "anthropic", "gemini", "openrouter", "deepseek", "zai", "glm", "ollama-local"} <= set(by_id)
    assert by_id["openrouter"]["group"] == "aggregator" and by_id["ollama-local"]["group"] == "local"
    assert by_id["openai"]["base_url"] == "https://api.openai.com/v1" and by_id["openai"]["alias"] == "openai"
    assert by_id["anthropic"]["api_format"] == "anthropic" and by_id["anthropic"]["base_url"].endswith("/v1")
    assert by_id["gemini"]["api_format"] == "gemini" and by_id["gemini"]["base_url"].endswith("/v1beta")
    assert by_id["ollama-local"]["key_required"] is False and by_id["openai"]["key_required"] is True
    # OAuth, cookie, no-auth, search, image, cloud agent and cloud-account providers are not here
    for left_out in ("codex", "gemini-cli", "github", "kiro", "cursor", "chatgpt-web", "grok-web", "jina-reader"):
        assert left_out not in by_id, left_out
    for left_out in ("fal-ai", "runwayml", "bedrock", "vertex", "azure-openai", "oneminai", "dify", "voyage-ai"):
        assert left_out not in by_id, left_out


def test_model_catalog_adds_the_claude_subscription_and_the_two_custom_endpoints():
    by_id = {e["id"]: e for e in model_catalog.providers()}
    assert by_id["claude"]["via"] == "login" and by_id["claude"]["group"] == "login"
    for custom, api_format in (("custom-openai", "openai"), ("custom-anthropic", "anthropic")):
        assert by_id[custom]["group"] == "custom" and by_id[custom]["via"] == "gateway"
        assert by_id[custom]["api_format"] == api_format and by_id[custom]["base_url"] == ""
    assert [e["id"] for e in model_catalog.providers()][:3] == ["claude", "custom-openai", "custom-anthropic"]
    assert model_catalog.find("zai")["name"] == "Z.AI" and model_catalog.find("nowhere") is None


# The logos (scripts/generate_provider_logos.py, drawn by web/src/components/accounts/provider-mark.tsx)

LOGOS = ROOT / "web" / "public" / "provider-logos"


def logo_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_provider_logos", ROOT / "scripts/generate_provider_logos.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_provider_of_the_catalog_is_mapped_to_a_logo_or_to_none():
    generator = logo_generator()
    planned = generator.plan([e["id"] for e in model_catalog.providers()], generator.LOGOS)
    manifest = json.loads((ROOT / "web/src/components/accounts/provider-logos.json").read_text())
    assert set(manifest) == set(planned), "the manifest is stale: run scripts/generate_provider_logos.py"
    assert len(manifest) >= 190 and {"openai", "anthropic", "claude", "gemini", "deepseek", "glm"} <= set(manifest)
    for id, file in manifest.items():
        assert re.fullmatch(r"[a-z0-9-]+\.(svg|png)", file) and (LOGOS / file).is_file(), id
        assert file.split(".")[0] == planned[id][1], id
    notice = (LOGOS / "NOTICE.md").read_text()
    for file in set(manifest.values()):
        assert f"`{file}`" in notice, file
    on_disk = {path.name for path in LOGOS.iterdir() if path.suffix in {".svg", ".png"}}
    assert on_disk == set(manifest.values()), "a logo no provider uses, or one missing"


def test_logo_plan_refuses_a_catalog_it_does_not_map_and_shares_one_file_per_source():
    generator = logo_generator()
    plan = generator.plan(["a", "b", "c", "d"], {"a": "lobe:x", "b": None, "c": "lobe:x", "d": "https://d.test/i.png"})
    assert plan == {"a": ("lobe:x", "a"), "c": ("lobe:x", "a"), "d": ("https://d.test/i.png", "d")}
    with pytest.raises(SystemExit, match=r"unmapped \['new'\]"):
        generator.plan(["a", "new"], {"a": None})
    with pytest.raises(SystemExit, match=r"not in the catalog \['gone'\]"):
        generator.plan(["a"], {"a": None, "gone": None})


def test_logo_svgs_scale_and_do_not_follow_the_colour_scheme():
    generator = logo_generator()
    fixed = generator.check_svg(
        "t", b'<svg xmlns="http://www.w3.org/2000/svg" width="32" height="16"><path d="M0 0h1"/></svg>'
    )
    assert b'<svg viewBox="0 0 32 16" xmlns=' in fixed
    with pytest.raises(SystemExit, match="colour scheme"):
        generator.check_svg("t", b'<svg viewBox="0 0 1 1"><style>@media (prefers-color-scheme: dark){}</style></svg>')
    with pytest.raises(SystemExit, match="neither a viewBox"):
        generator.check_svg("t", b"<svg><path/></svg>")
    for path in LOGOS.glob("*.svg"):
        text = path.read_text()
        assert "viewBox=" in text and "prefers-color-scheme" not in text and "<script" not in text.lower(), path.name


# The generator's filter, on rows

SOURCE = {"name": "OmniRoute", "version": "9.9.9"}


def test_model_catalog_generator_turns_endpoints_into_roots_and_sorts_into_groups():
    build = generator().build
    rows = [
        row("zeta", family="regional"),
        row("alpha", family="gateways"),
        row("claudish", registry={"format": "claude", "baseUrl": "https://c.test/v1/messages?beta=true"}),
        row("googly", registry={"format": "gemini", "baseUrl": "https://g.test/v1beta/models"}),
        row("freebie", hasFree=True),
        row("viaset", sets=["aggregator"]),
        row("inf", family="inference"),
        row("lm", family="local", localDefault="http://localhost:1234/v1", registry={"baseUrl": None}),
        row("opt", registry={"authType": "optional"}),
    ]
    catalog = build(rows, SOURCE)
    by_id = {e["id"]: e for e in catalog["providers"]}
    assert [e["id"] for e in catalog["providers"][3:]] == sorted(
        [r["id"] for r in rows], key=lambda i: by_id[i]["name"].lower()
    )
    assert by_id["claudish"]["base_url"] == "https://c.test/v1" and by_id["claudish"]["api_format"] == "anthropic"
    assert (
        by_id["googly"]["base_url"] == "https://g.test/v1beta" and by_id["alpha"]["base_url"] == "https://alpha.test/v1"
    )
    groups = {i: by_id[i]["group"] for i in by_id}
    assert groups["zeta"] == "regional" and groups["alpha"] == "aggregator" and groups["viaset"] == "aggregator"
    assert groups["inf"] == "inference" and groups["lm"] == "local" and groups["freebie"] == "free"
    assert by_id["lm"]["base_url"] == "http://localhost:1234/v1" and by_id["lm"]["key_required"] is False
    assert by_id["opt"]["key_required"] is False and by_id["zeta"]["key_required"] is True
    assert by_id["zeta"]["key_hint"] == "Create an API key on https://zeta.test."
    assert catalog["source"] == SOURCE


@pytest.mark.parametrize(
    "left_out",
    [
        row("oauthy", registry={"authType": "oauth"}),
        row("cookie", registry={"authType": "none"}),
        row("searchy", serviceKinds=["webFetch"]),
        row("pic", sets=["imageOnly"]),
        row("vid", sets=["video"]),
        row("cloud", sets=["enterprise"]),
        row("old", deprecated=True),
        row("risky", subscriptionRisk=True),
        row("hidden", hiddenFromDashboard=True),
        row("weird", registry={"format": "cursor"}),
        row("vertexy", registry={"executor": "vertex"}),
        row("templated", registry={"baseUrl": "https://{account}.test/api"}),
        row("nourl", registry={"baseUrl": ""}),
        row("unregistered") | {"registry": None},
        row("oneminai"),
    ],
    ids=lambda r: r["id"],
)
def test_model_catalog_generator_leaves_out_what_a_key_does_not_reach(left_out):
    assert [e["id"] for e in generator().build([left_out], SOURCE)["providers"] if e["via"] == "gateway"][3:] == []


def test_model_catalog_generator_refuses_two_providers_of_one_id():
    with pytest.raises(SystemExit, match="same id: dup"):
        generator().build([row("dup"), row("dup", family="regional")], SOURCE)


# The route and the command

if pg.DSN:
    app_key, hub = secrets_cli.app_key, secrets_cli.hub


@needs_pg
def test_model_catalog_route_answers_a_member_and_a_stranger_gets_nothing(hub):
    owner = hub.headers["owner"]
    found = hub.client.get("/v1/model-catalog", headers=owner)
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["source"]["version"] == model_catalog.GATEWAY_VERSION and body["groups"][0] == "login"
    assert len(body["providers"]) == len(model_catalog.providers())
    assert found.headers["cache-control"] == "private, max-age=3600"
    again = hub.client.get("/v1/model-catalog", headers={**owner, "If-None-Match": found.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    local = hub.client.get("/v1/model-catalog?group=local", headers=owner).json()["providers"]
    assert local and {p["group"] for p in local} == {"local"}
    assert hub.client.get("/v1/model-catalog").status_code == 401
    assert "/v1/model-catalog" in hub.client.get("/v1/openapi.json").json()["paths"]


def test_model_catalog_command_is_in_the_contract():
    command = commands()["hub model-catalog"]
    assert "--group" in str(command) and command["json"]["keys"] == ["source", "groups", "providers"]


@needs_pg
def test_model_catalog_command_prints_the_table_and_the_json(hub, monkeypatch, capsys):
    home = hub.homes["owner"]
    printed = ok(run_cli(monkeypatch, capsys, home, "hub", "model-catalog", "--group", "regional"))
    assert "zai" in printed.out and "openrouter" not in printed.out and "OmniRoute 3.8.52" in printed.out
    found = as_json(run_cli(monkeypatch, capsys, home, "hub", "model-catalog", "--json"))
    assert_json_keys("hub model-catalog", found)
    assert {p["id"] for p in found["providers"]} == {e["id"] for e in model_catalog.providers()}
