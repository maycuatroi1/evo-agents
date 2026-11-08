"""The hub runtime (``evo_agents.hub.server.hub_runtime`` and ``hub_tools``), as criteria a6 and a13 of plan
multi-agent-systems name them.

a6: an agent whose profile runs on the hub (``runs_on: hub``) answers in chat through the hub's model gateway: each
turn is a run of runtime hub that a job of the hub's queue takes (``hub.hub_turn``), with no worker and no pool; its
tools post, send an interactive answer and ask; the tool loop is bounded in rounds; a follow-up in the thread wakes the
agent; each turn's tokens and cost reach run_spend, the run's usage and its invoke_agent span; a system's hub agents
stop at its ``limits.daily_usd`` and its owner hears of it once; without a gateway the agent says the runtime is not
configured. a13: an ask of a hub agent parks its run, and the end of the run asked wakes it with the answer.

The gateway is ``Relaying`` of test_gateway_runs.py, whose /v1/chat/completions answers what a test scripts.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import column, insert, select, table
from sqlalchemy.dialects.postgresql import JSONB

from evo_agents.hub import chat_runs, system_spec, tables
from evo_agents.hub.jobs import HUB_TURN
from evo_agents.hub.server import agent_tasking, hub_runtime, pool_accounts
from evo_agents.hub.server.profile_store import by_handle
from tests.hub.account_pool_setup import give_account_pool
from tests.hub.live import sql
from tests.hub.test_chat_runs import created, sent, thread
from tests.hub.test_gateway import _app, settled
from tests.hub.test_gateway_runs import MODEL, Relaying
from tests.hub.test_plans import registration
from tests.hub.test_provider_accounts import OWNER, PROJECT, account, sample, written
from tests.hub.test_provider_accounts import hub as hub  # noqa: F401  the fixture

KB, KB_AGENT = "lumen-kb", "kb-bot"
QUESTION = "Which service of lumen-kb signs the export links?"
ANSWER = "The export service signs them; it reads the key in kb/export/keys.py::load_key."
USAGE = {"prompt_tokens": 100, "completion_tokens": 20}  # each answer of the scripted model
procrastinate_jobs = table("procrastinate_jobs", column("id"), column("task_name"), column("args", JSONB))
KEY_COST = 0.0125  # what FakeGateway's usage of a key says it cost


class Talking(Relaying):
    """The gateway whose /v1/chat/completions answers the messages ``script`` holds, in order (a final answer once it
    is empty), and keeps each request's body in ``asked``."""

    def __init__(self) -> None:
        super().__init__()
        self.script: list[dict] = []
        self.asked: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return super().__call__(request)
        self.relayed.append(request)
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.key_values():
            return httpx.Response(401, json={"error": "invalid key"})
        self.asked.append(json.loads(request.content))
        message = self.script.pop(0) if self.script else {"content": "That is all."}
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": USAGE})


def tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.fixture
def gateway() -> Talking:
    return Talking()


@pytest.fixture
def client(hub_db, tmp_path, github, gateway, s3):
    with TestClient(_app(hub_db, tmp_path, github, gateway, **s3.config()), base_url="https://hub.test") as client:
        yield client


def hub_agent(client, db, headers, handle: str = "helper", project: str = PROJECT, account_name: str = "or") -> dict:
    """Agent ``handle`` of ``project`` that runs on the hub on MODEL, with the provider account ``account_name`` in its
    account pool, and no worker pool."""
    permissions = {"model": {"mode": "allowlist", "allow": [MODEL]}}
    body = {"handle": handle, "name": handle.title(), "runs_on": "hub", "permissions": permissions}
    response = client.post(
        f"/v1/projects/{project}/agents", json={**body, "instructions": "Be brief."}, headers=headers
    )
    assert response.status_code == 201, response.text
    give_account_pool(db, project, handle, OWNER)
    ap, m, s = tables.agent_profiles, tables.account_pool_members, tables.secrets
    (pool_id,) = sql(db, select(ap.c.account_pool_id).where(ap.c.handle == handle))[0]
    (secret_id, owner_id) = sql(
        db, select(s.c.id, s.c.owner_id).where(s.c.name == account_name, s.c.deleted_at.is_(None))
    )[0]
    sql(db, insert(m).values(pool_id=pool_id, secret_id=secret_id, added_by=owner_id))
    return response.json()


@pytest.fixture
def helper(client, hub, hub_db) -> dict:  # noqa: F811
    """@helper of the project, on the hub, and the provider account of openrouter it reaches at the gateway."""
    written(client, hub["owner"], "or", account(sample(), models=["auto"]))
    settled(client)
    return hub_agent(client, hub_db, hub["owner"])


def turn(client, gateway, run_id: int, **config_changes) -> dict:
    """Take the turn of run ``run_id`` as the job ``hub.hub_turn`` does, through ``gateway``."""
    state = client.app.state
    config = dataclasses.replace(state.config, **config_changes) if config_changes else state.config
    transport = httpx.MockTransport(gateway)
    return client.portal.call(
        lambda: hub_runtime.take_turn(state.engine, config, state.blobs, state.sealer, run_id, transport=transport)
    )


def run_row(db, run_id: int):
    r = tables.runs
    query = select(
        r.c.state,
        r.c.requested_runtime,
        r.c.runtime,
        r.c.worker_id,
        r.c.model,
        r.c.hop,
        r.c.spawned_by_run_id,
        r.c.resume_of_run_id,
    )
    return sql(db, query.where(r.c.id == run_id))[0]


def chat_runs_of(db) -> list[int]:
    r = tables.runs
    return [row[0] for row in sql(db, select(r.c.id).where(r.c.kind == "chat").order_by(r.c.id))]


def turn_jobs(db) -> list[int]:
    jobs = procrastinate_jobs.c
    query = select(jobs.args).where(jobs.task_name == HUB_TURN).order_by(jobs.id)
    return [row[0]["run_id"] for row in sql(db, query)]


def bodies(client, headers, chat: dict, root: int) -> list[str]:
    return [item["body"] for item in thread(client, headers, chat["id"], root)]


def put_system(client, headers, limits: dict | None = None) -> dict:
    spec = {
        "schema": system_spec.SCHEMA,
        "name": "Help desk",
        "purpose": "Answer the questions of the project.",
        "nodes": [{"id": "in", "kind": "entry"}, {"id": "help", "kind": "agent", "profile": "helper"}],
        "edges": [{"from": "in", "to": "help", "kind": "interacts", "description": "every question"}],
    }
    if limits is not None:
        spec["limits"] = limits
    response = client.put(f"/v1/projects/{PROJECT}/systems/desk", json={"spec": spec}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def invoke_span(db, run_id: int) -> dict:
    s = tables.system_spans
    query = select(s.c.attributes).where(s.c.run_id == run_id, s.c.name == "invoke_agent")
    return sql(db, query)[0][0]


# A turn


def test_a_hub_agent_answers_through_the_gateway_with_no_worker_and_records_what_it_spent(
    client, hub, hub_db, gateway, helper
):
    put_system(client, hub["owner"])
    chat = created(client, hub["owner"], agents=["helper"])
    mention = sent(client, hub["owner"], chat["id"], "@helper which service signs the identity tokens?")
    (run_id,) = chat_runs_of(hub_db)
    state, requested, runtime, worker_id, model, *_ = run_row(hub_db, run_id)
    assert (state, requested, runtime, worker_id, model) == ("queued", "hub", "hub", None, MODEL)
    assert turn_jobs(hub_db) == [run_id]

    page = "<!doctype html><html><body><p>The token service</p></body></html>"
    gateway.script = [
        {
            "content": None,
            "tool_calls": [
                tool_call("chat_post", {"body": "Looking it up."}, "call-1"),
                tool_call("interactive_answer", {"html": page, "caption": "The signer"}, "call-2"),
                tool_call("no_such_tool", {}, "call-3"),
            ],
        },
        {"content": "lumen-identity's token service signs them."},
    ]
    done = turn(client, gateway, run_id)
    assert (done["state"], done["rounds"], done["input"], done["output"]) == ("done", 2, 200, 40)
    assert done["cost_usd"] == pytest.approx(KEY_COST)

    first = gateway.asked[0]
    assert first["model"] == MODEL
    assert {tool["function"]["name"] for tool in first["tools"]} >= {
        "kg_search",
        "chat_post",
        "interactive_answer",
        "agent_ask",
        "agent_forward",
        "escalate",
        "memory_write",
    }
    system, user = first["messages"][0]["content"], first["messages"][1]["content"]
    assert system.startswith("Be brief.") and "# On the hub" in system
    assert chat_runs.ASK_COMMAND in user and "which service signs the identity tokens" in user
    results = [m for m in gateway.asked[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["call-1", "call-2", "call-3"]
    assert results[0]["content"].startswith("posted message #")
    assert results[1]["content"].startswith("sent the interactive answer as output #")
    assert results[2]["content"].startswith("error: no tool no_such_tool")
    assert gateway.keys == {}  # the turn's key is revoked

    shown = bodies(client, hub["owner"], chat, mention["id"])
    assert "Looking it up." in shown and "lumen-identity's token service signs them." in shown
    o = tables.run_outputs
    assert sql(hub_db, select(o.c.name, o.c.interactive).where(o.c.run_id == run_id)) == [("answer.html", True)]

    r, rs = tables.runs, tables.run_spend
    spend = sql(hub_db, select(rs.c.model, rs.c.input_tokens, rs.c.output_tokens, rs.c.reported_usd))
    assert spend == [(MODEL, 200, 40, pytest.approx(KEY_COST))]
    (usage,) = sql(hub_db, select(r.c.usage).where(r.c.id == run_id))[0]
    assert usage["total_cost_usd"] == pytest.approx(KEY_COST)
    assert (usage["gateway"]["prompt_tokens"], usage["gateway"]["completion_tokens"]) == (200, 40)
    attributes = invoke_span(hub_db, run_id)
    assert attributes["gen_ai.usage.input_tokens"] == 200 and attributes["gen_ai.usage.output_tokens"] == 40
    assert attributes["evo.cost_usd"] == pytest.approx(KEY_COST)
    assert attributes["gen_ai.agent.name"] == "helper"  # what the span held stays

    # a follow-up in the thread wakes the agent in a new run of the hub runtime
    sent(client, hub["owner"], chat["id"], "@helper and who rotates its key?", reply_to=mention["id"])
    (_, follow) = chat_runs_of(hub_db)
    assert run_row(hub_db, follow)[:4] == ("queued", "hub", "hub", None)
    assert run_row(hub_db, follow)[7] == run_id
    assert turn_jobs(hub_db) == [run_id, follow]
    assert turn(client, gateway, follow)["state"] == "done"
    assert "who rotates its key" in json.dumps(gateway.asked[-1]["messages"])
    assert turn(client, gateway, follow) == {"run_id": follow, "state": None}  # a job run twice takes no turn


def test_the_tool_loop_stops_after_its_rounds(client, hub, hub_db, gateway, helper, monkeypatch):
    monkeypatch.setattr(hub_runtime, "MAX_ROUNDS", 2)
    chat = created(client, hub["owner"], agents=["helper"])
    sent(client, hub["owner"], chat["id"], "@helper loop forever")
    (run_id,) = chat_runs_of(hub_db)
    gateway.script = [{"content": None, "tool_calls": [tool_call("no_such_tool", {})]}] * 3
    done = turn(client, gateway, run_id)
    assert (done["state"], done["rounds"]) == ("done", 2)
    assert len(gateway.asked) == 2


def test_a_hub_agent_needs_no_pool_and_says_when_the_hub_has_no_gateway(client, hub, hub_db, gateway, helper):
    async def read():
        async with client.app.state.engine.connect() as conn:
            profile = await by_handle(conn, "helper")
            return (
                profile,
                await agent_tasking.work_home(conn, profile),
                await agent_tasking.takes_work(conn, [profile.id]),
            )

    profile, home, working = client.portal.call(read)
    assert home == (None, None) and profile.id in working
    assert pool_accounts.chat_needs_pool(profile, PROJECT) is None

    chat = created(client, hub["owner"], agents=["helper"])
    mention = sent(client, hub["owner"], chat["id"], "@helper are you there?")
    (run_id,) = chat_runs_of(hub_db)
    assert turn(client, gateway, run_id, gateway_secret=None)["state"] == "done"
    assert hub_runtime.OFF.format(handle="helper") in bodies(client, hub["owner"], chat, mention["id"])
    assert gateway.asked == []


def test_a_system_at_its_daily_limit_stops_its_hub_agents_and_tells_its_owner_once(
    client, hub, hub_db, gateway, helper
):
    put_system(client, hub["owner"], limits={"daily_usd": 0.01})
    chat = created(client, hub["owner"], agents=["helper"])
    sent(client, hub["owner"], chat["id"], "@helper first question")
    (first,) = chat_runs_of(hub_db)
    assert turn(client, gateway, first)["state"] == "done"  # 0.0125 USD: past the limit

    for text in ("@helper second question", "@helper third question"):
        mention = sent(client, hub["owner"], chat["id"], text)
        run_id = chat_runs_of(hub_db)[-1]
        assert turn(client, gateway, run_id)["state"] == "done"
        shown = bodies(client, hub["owner"], chat, mention["id"])
        assert any(item.startswith("I take no turn now: the hub agents of system desk spent 0.01") for item in shown)
    assert len(gateway.asked) == 1
    n, u = tables.notifications, tables.users
    owner = select(u.c.id).where(u.c.login == OWNER).scalar_subquery()
    told = sql(hub_db, select(n.c.title).where(n.c.notice_kind == "system_daily_cap", n.c.user_id == owner))
    assert told == [("System desk reached its daily limit",)]


# An ask (a13)


def test_an_ask_parks_the_hub_run_and_the_answer_wakes_it(client, hub, hub_db, gateway, helper):
    assert client.put(f"/v1/projects/{KB}", json=registration(), headers=hub["admin"]).status_code == 200
    admin = {"role": "admin", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{KB}/grants/{OWNER}", json=admin, headers=hub["admin"]).status_code == 200
    written(client, hub["owner"], "kb", account(sample(), models=["auto"], projects=[KB]))
    settled(client)
    hub_agent(client, hub_db, hub["owner"], KB_AGENT, KB, "kb")

    chat = created(client, hub["owner"], agents=["helper"])
    mention = sent(client, hub["owner"], chat["id"], "@helper ask the kb team who signs the export links")
    (parent,) = chat_runs_of(hub_db)
    brief = {"goal": "name the signer", "output": "a service and a path", "done_when": "it names both"}
    gateway.script = [
        {"content": None, "tool_calls": [tool_call("agent_ask", {"handle": KB_AGENT, "body": QUESTION, **brief})]},
        {"content": None, "tool_calls": [tool_call("kg_search", {"query": "export links"})]},
    ]
    asked = turn(client, gateway, parent)
    assert asked["state"] == "parked" and len(asked["asked"]) == 1
    assert (asked["rounds"], len(gateway.asked)) == (1, 1)  # the ask ended the turn: no search after it
    (child,) = asked["asked"]
    state, requested, _, worker_id, _, hop, spawned_by, _ = run_row(hub_db, child)
    assert (state, requested, worker_id, hop, spawned_by) == ("queued", "hub", None, 2, parent)
    assert run_row(hub_db, parent)[0] == "parked"
    assert child in turn_jobs(hub_db)

    gateway.script = [{"content": ANSWER}]
    assert turn(client, gateway, child)["state"] == "done"
    d = tables.decisions
    assert sql(hub_db, select(d.c.state).where(d.c.run_id == parent, d.c.category == "wait")) == [("answered",)]
    r = tables.runs
    (woken,) = [row[0] for row in sql(hub_db, select(r.c.id).where(r.c.resume_of_run_id == parent))]
    assert run_row(hub_db, woken)[:4] == ("queued", "hub", "hub", None)
    assert woken in turn_jobs(hub_db)

    gateway.script = [{"content": "kb-bot says the export service signs them, in kb/export/keys.py."}]
    assert turn(client, gateway, woken)["state"] == "done"
    assert "load_key" in json.dumps(gateway.asked[-1]["messages"])
    shown = bodies(client, hub["owner"], chat, mention["id"])
    assert "kb-bot says the export service signs them, in kb/export/keys.py." in shown
