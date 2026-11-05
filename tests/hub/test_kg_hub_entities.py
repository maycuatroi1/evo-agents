"""The entities step of a build on the hub with a url model (plan kg-zip-to-answers, step 2, criterion a3): the build
calls the OpenAI-compatible endpoint itself with the key it was handed in memory (``kg_build.build_graph``,
``entity_keys``), never through the environment; items above the sink's clearance are not sent; the ceilings hold; the
key is in no coverage, log line, graph, memo or prompt, and an error that quotes it is kept without it. The built-in
strategy multi-hop-api is that model, and multi-hop keeps Claude Code. The hub's side that opens the key from a secret
(``evo_agents.hub.server.kg_entity_key``) runs against Postgres when EVO_HUB_TEST_DSN is set."""

import asyncio
import gzip
import json
import logging
import os
import secrets as random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import func, insert

from evo_agents.hub import kg_build, tables
from evo_agents.kg import entities, strategy
from evo_agents.kg import settings as kg_settings
from tests.hub import pg
from tests.hub.live import sql
from tests.kg.test_extract_entities import entity_nodes, setup

KEY = "sk-or-v1-test-0123456789abcdef"  # not a real key: what the tests look for everywhere
ENV = "OPENROUTER_API_KEY"
URL = "https://openrouter.ai/api/v1"


@pytest.fixture
def kg_env(tmp_path, monkeypatch):
    home = tmp_path / "kg-home"
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    monkeypatch.delenv(ENV, raising=False)
    return home


class Endpoint:
    """An OpenAI-compatible endpoint as the ``openai`` client reaches it: it keeps the key each client was made with and
    the prompts it was sent, and answers as the model of ``tests.kg.test_extract_entities`` does."""

    def __init__(self, fail: str | None = None):
        self.keys: list[str | None] = []
        self.prompts: list[str] = []
        self.fail = fail

    def client(self, config, key=None):
        self.keys.append(key)
        endpoint = self

        class Completions:
            @staticmethod
            def create(**kwargs):
                text = kwargs["messages"][0]["content"]
                endpoint.prompts.append(text)
                if endpoint.fail:
                    raise RuntimeError(endpoint.fail)
                name = "Search" if "Intro" in text else "Obsolete"
                answer = {
                    "nodes": [{"kind": "Concept", "name": name, "conf": 0.8}, {"kind": "Concept", "name": "Server"}],
                    "edges": [{"src": name, "rel": "depends_on", "dst": "Server"}],
                }
                message = type("M", (), {"content": json.dumps(answer)})
                usage = type("U", (), {"prompt_tokens": 1000, "completion_tokens": 200})
                return type("Got", (), {"choices": [type("C", (), {"message": message})], "usage": usage})

        return type("Client", (), {"chat": type("Chat", (), {"completions": Completions})})


def url_project(tmp_path, kg_env, **extra):
    """The project of ``tests.kg.test_extract_entities`` with its entities mapping on the url model of multi-hop-api."""
    project, _ = setup(tmp_path, kg_env, sink="llm")
    before = kg_settings.read(project.root)
    mapping = {k: v for k, v in before.entities.items() if k != "command"}
    model = strategy.builtins()["multi-hop-api"].settings["entities"]
    mapping |= {k: model[k] for k in ("model", "url", "key_env", "price")} | extra
    kg_settings.write(project.root, kg_settings.of({"entities": mapping}, before), reason="the url model")
    return project


def everything_under(*roots: Path) -> bytes:
    return b"".join(p.read_bytes() for root in roots for p in sorted(root.rglob("*")) if p.is_file())


def test_a_hub_build_calls_the_endpoint_with_the_key_in_memory_and_writes_it_nowhere(
    tmp_path, kg_env, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    endpoint = Endpoint()
    monkeypatch.setattr(entities, "CLIENT_FACTORY", endpoint.client)
    project = url_project(tmp_path, kg_env)
    path, report = kg_build.build_graph(project, entity_keys={ENV: KEY})
    assert report.ok, report.errors
    cov = report.coverage["entities"]
    assert cov["calls"] == 3 == len(endpoint.prompts) and endpoint.keys == [KEY] * 3
    assert cov["sink"] == "llm" and cov["local"] is False and cov["skipped"] == 0
    assert cov["tokens_in"] == 3000 and cov["cost"] == pytest.approx(3 * (1000 * 0.1 + 200 * 0.5) / 1e6)
    assert {r[0] for r in entity_nodes(project)} >= {"entity:Concept:search", "entity:Concept:server"}
    assert ENV not in os.environ, "the key never reaches the environment of the build"
    assert KEY not in json.dumps(report.coverage) and KEY not in caplog.text
    assert not any(KEY in p for p in endpoint.prompts)
    assert KEY.encode() not in everything_under(kg_env, tmp_path), "nor the graph, the memo or the corpus"


def test_without_a_key_the_url_model_is_not_called_and_the_items_wait(tmp_path, kg_env, monkeypatch):
    endpoint = Endpoint()
    monkeypatch.setattr(entities, "CLIENT_FACTORY", endpoint.client)
    project = url_project(tmp_path, kg_env, jobs=2)
    _, report = kg_build.build_graph(project)
    cov = report.coverage["entities"]
    assert report.ok and endpoint.keys == [] and cov["calls"] == 0 and cov["skipped"] == 3
    assert f"no key: {ENV}" in cov["stopped"]
    assert entities.unfinished(report.coverage), "the next build goes on once a key is there"


def test_an_endpoint_error_that_quotes_the_key_is_kept_without_it(tmp_path, kg_env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    endpoint = Endpoint(fail=f"401 Incorrect API key provided: {KEY}")
    monkeypatch.setattr(entities, "CLIENT_FACTORY", endpoint.client)
    project = url_project(tmp_path, kg_env)
    _, report = kg_build.build_graph(project, entity_keys={ENV: KEY})
    cov = report.coverage["entities"]
    assert report.ok and cov["failed"] == 3
    assert "[key]" in cov["errors"][0]["error"]
    assert KEY not in json.dumps(report.coverage) and KEY not in caplog.text


def test_an_item_above_the_clearance_of_the_sink_is_not_sent(tmp_path, kg_env, monkeypatch):
    endpoint = Endpoint()
    monkeypatch.setattr(entities, "CLIENT_FACTORY", endpoint.client)
    project = url_project(tmp_path, kg_env, sink="llm-low")
    _, report = kg_build.build_graph(project, entity_keys={ENV: KEY})
    cov = report.coverage["entities"]
    assert endpoint.prompts == [] and cov["calls"] == 0 and cov["refused_count"] >= 2
    assert "above the clearance of sink 'llm-low'" in cov["refused"][0]["reason"]


@pytest.mark.parametrize("ceiling, why", [({"max_calls": 1}, "max_calls 1"), ({"max_tokens": 1000}, "max_tokens")])
def test_the_ceilings_of_a_build_hold_for_the_url_model(tmp_path, kg_env, monkeypatch, ceiling, why):
    endpoint = Endpoint()
    monkeypatch.setattr(entities, "CLIENT_FACTORY", endpoint.client)
    project = url_project(tmp_path, kg_env, **ceiling)
    _, report = kg_build.build_graph(project, entity_keys={ENV: KEY})
    cov = report.coverage["entities"]
    assert cov["calls"] == 1 == len(endpoint.prompts) and cov["skipped"] == 2 and why in cov["stopped"]


def test_multi_hop_api_reaches_openrouter_through_the_sink_llm_with_the_ceilings_of_multi_hop():
    found = strategy.builtins()
    api, claude = found["multi-hop-api"], found["multi-hop"]
    mapping = api.settings["entities"]
    assert api.steps == ("entities",) and "prose" in api.suits
    assert mapping["url"] == URL and mapping["key_env"] == ENV and mapping["sink"] == "llm"
    assert "command" not in mapping and set(mapping["price"]) == {"input", "output"}
    for key in ("max_cost", "max_calls", "max_tokens", "node_kinds", "relations"):
        assert mapping.get(key) == claude.settings["entities"].get(key), key
    assert api.ontology == claude.ontology
    assert claude.settings["entities"]["command"] == ["evo-agents", "kg", "claude-entities"], "multi-hop is as it was"
    assert "url" not in claude.settings["entities"]
    assert entities.check(mapping) == mapping


# --- The key from a secret on the hub (kg_entity_key), against Postgres ---

MAPPING = strategy.builtins()["multi-hop-api"].settings["entities"]


def hub_rows(db):
    """Members ``creator`` (who made the project) and ``other``, and the project: their ids."""
    u, p = tables.users, tables.projects
    creator = sql(db, insert(u).values(login="creator").returning(u.c.id))[0][0]
    other = sql(db, insert(u).values(login="other").returning(u.c.id))[0][0]
    project = sql(
        db,
        insert(p)
        .values(
            name="prose",
            levels=["public", "internal", "confidential"],
            locations=["hub"],
            default_label={"level": "internal"},
            created_by=creator,
        )
        .returning(p.c.id),
    )[0][0]
    return creator, other, project


def put_secret(db, sealer, owner, name, value, *, kind="env", env_var=ENV, base_url=None, bind=None, **extra):
    from evo_agents.hub.server.sealing import secret_aad

    s = tables.secrets
    sealed = sealer.seal(value, secret_aad(owner, name, kind))
    row = {"owner_id": owner, "name": name, "kind": kind, "sealed": sealed.ciphertext, "nonce": sealed.nonce}
    row |= {"key_id": sealed.key_id, "env_var": env_var if kind == "env" else None} | extra
    if "deleted_at" in extra:  # deleting a secret drops its value
        row |= {"sealed": None, "nonce": None, "key_id": None}
    secret_id = sql(db, insert(s).values(row).returning(s.c.id))[0][0]
    if base_url:
        account = {"secret_id": secret_id, "provider": "openrouter", "base_url": base_url, "api_format": "openai"}
        sql(db, insert(tables.provider_accounts).values(account))
    if bind:
        sql(db, insert(tables.secret_bindings).values(secret_id=secret_id, project_id=bind))
    return secret_id


def find(db, sealer, project, mapping=MAPPING):
    import psycopg
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from evo_agents.hub.server import kg_entity_key

    async def run():
        engine = create_async_engine(
            "postgresql+psycopg://", poolclass=NullPool, async_creator=lambda: psycopg.AsyncConnection.connect(db.dsn)
        )
        try:
            async with engine.connect() as conn:
                return await kg_entity_key.find(conn, sealer, project, mapping)
        finally:
            await engine.dispose()

    return asyncio.run(run())


needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


@needs_pg
def test_the_build_opens_the_creators_key_and_prefers_one_bound_to_the_project(hub_db):
    from evo_agents.hub.server.sealing import Sealer

    sealer = Sealer(random.token_bytes(32))
    creator, other, project = hub_rows(hub_db)
    assert find(hub_db, sealer, project) == ({}, None)
    assert find(hub_db, None, project) == ({}, None), "a hub without a sealing key opens nothing"
    put_secret(hub_db, sealer, other, "theirs", "sk-other")  # another member's, not bound: not theirs to spend
    gone = datetime.now(UTC) - timedelta(hours=1)
    put_secret(hub_db, sealer, creator, "old", "sk-old", expires_at=gone)
    put_secret(hub_db, sealer, creator, "deleted", "sk-deleted", deleted_at=func.now())
    put_secret(hub_db, sealer, creator, "elsewhere", "sk-else", env_var="OTHER_KEY")
    put_secret(hub_db, sealer, creator, "acct-x", "sk-x", kind="provider", base_url="https://api.example.com/v1")
    assert find(hub_db, sealer, project) == ({}, None)
    account = put_secret(
        hub_db, sealer, creator, "acct", "sk-acct", kind="provider", base_url="https://openrouter.ai/api"
    )
    assert find(hub_db, sealer, project) == ({ENV: "sk-acct"}, f"secret:{account}"), "a provider account on the host"
    own = put_secret(hub_db, sealer, creator, "mine", " sk-mine\n")
    assert find(hub_db, sealer, project) == ({ENV: "sk-mine"}, f"secret:{own}"), "the env kind first"
    bound = put_secret(hub_db, sealer, other, "team", "sk-team", bind=project)
    assert find(hub_db, sealer, project) == ({ENV: "sk-team"}, f"secret:{bound}"), "a key bound to the project first"
    assert find(hub_db, Sealer(random.token_bytes(32)), project) == ({}, None), "a value it cannot open is passed over"
    assert find(hub_db, sealer, project, {**MAPPING, "key_env": None}) == ({}, None)
    assert find(hub_db, sealer, project, strategy.builtins()["multi-hop"].settings["entities"]) == ({}, None)


@needs_pg
def test_a_project_made_by_upload_runs_its_entities_in_the_hub_build_with_its_creators_key(
    hub_db, tmp_path, s3, monkeypatch, caplog
):
    """No worker run: the upload, the settings of multi-hop-api, a secret of the creator, and the hub's own jobs."""
    from sqlalchemy import select

    from evo_agents.hub.server.sealing import Sealer
    from tests.hub import live, test_kg
    from tests.hub.test_kg_upload_extract import extract, open_hub
    from tests.hub.test_kg_upload_project import NEW, notes, project_row, send

    caplog.set_level(logging.DEBUG)
    monkeypatch.delenv(ENV, raising=False)
    sealer = Sealer(random.token_bytes(32))
    monkeypatch.setattr(test_kg, "HubContext", lambda *a, **k: kg_hub_context(*a, sealer=sealer, **k))
    endpoint = Endpoint()
    monkeypatch.setattr(entities, "CLIENT_FACTORY", endpoint.client)
    client, hub = open_hub(hub_db, tmp_path, s3)
    try:
        assert send(hub, hub.admin, notes(tmp_path))["state"] == "extracting"
        project = project_row(hub)[0]
        values = {**strategy.builtins()["multi-hop-api"].settings, "strategy": "multi-hop-api"}
        put = hub.client.put(f"/v1/kg/{NEW}/settings", json={"values": values}, headers=hub.admin)
        assert put.status_code == 200, put.text
        creator = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == live.ADMIN))[0][0]
        secret = put_secret(hub_db, sealer, creator, "openrouter", KEY)
        assert extract(hub) == {"extracted": 1}
        test_kg.run_jobs(hub.db, hub.s3, hub.worker_dir)
    finally:
        client.__exit__(None, None, None)
    b = tables.kg_builds
    rows = sql(hub_db, select(b.c.status, b.c.coverage, b.c.error).where(b.c.project_id == project).order_by(b.c.id))
    status, coverage, error = rows[-1]
    assert status == "succeeded", error
    cov = coverage["entities"]
    assert cov["key_from"] == f"secret:{secret}" and cov["sink"] == "llm" and cov["calls"] >= 1
    assert endpoint.keys and set(endpoint.keys) == {KEY} and not any(KEY in p for p in endpoint.prompts)
    assert KEY not in json.dumps(coverage), "nor the coverage"
    assert KEY not in caplog.text, "nor a log line"
    assert ENV not in os.environ, "nor the environment of the worker"
    assert not any(KEY.encode() in opened(s3.get(name)) for name in s3.keys()), "no blob holds the key"


def opened(data: bytes | None) -> bytes:
    """A blob's bytes, unpacked when gzip packed them."""
    try:
        return gzip.decompress(data or b"")
    except (OSError, EOFError):
        return data or b""


def kg_hub_context(*args, sealer, **kwargs):
    from evo_agents.hub.worker import HubContext

    return HubContext(*args, sealer=sealer, **kwargs)
