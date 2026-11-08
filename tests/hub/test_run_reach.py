"""The run reach of a member (``run_access_rules.REACH_GROUPS``, ``users.run_reach``): the groups agents, workers,
curator, releases, answers and runs, which the tokens of the member's runs that run as them reach once the member opened them.

Empty, a run's token is refused there as before, with a refusal that says how the owner opens the group. Each group
opened lets exactly its paths through; the terminal, the live view, takeover and handback of a run, the worker
protocol, an agent's accounts, secrets, tokens, the run reach itself and every other group stay refused with all six
open. Only the member reads and sets their own, at GET and PUT /v1/autonomy/run-reach, each change audited
``run_access.reach``; the hub reads it with the run's token, so a change holds from the run's next request. The command
line is ``evo-agents hub autonomy run-reach``."""

import json

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi import HTTPException
from sqlalchemy import func, select

from evo_agents.hub import client as hub_client
from evo_agents.hub import tables
from evo_agents.hub.cli_autonomy import Usage, reach_groups
from evo_agents.hub.server import autonomy_store, run_access, run_access_rules
from evo_agents.hub.server.autonomy_models import RunReachSet
from evo_agents.hub.server.run_access_rules import REACH_GROUPS, refusal
from tests.hub import test_credentials_api as credentials_api
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_credentials_api import OWNER, PLAN, PROJECT, STEPS, worker_of
from tests.hub.test_run_access import (  # noqa: F401  (hub is a fixture: its default agent has an account pool)
    as_me_run,
    as_run,
    audit_of,
    chat_worker,
    command,
    hub,
    owner_admin,
    run_principal,
    saved_login,
    through,
    token_of,
)

app_key, config, client = (credentials_api.app_key, credentials_api.config, credentials_api.client)

ALL = frozenset(REACH_GROUPS)
REACH = "/v1/autonomy/run-reach"
# (method, path) of each group the owner may open: what its run's token reaches once they did
OPTIONAL = {
    "agents": (
        ("POST", "/v1/projects/p/agents"),
        ("PATCH", "/v1/projects/p/agents/reviewer"),
        ("DELETE", "/v1/projects/p/agents/reviewer"),
        ("PUT", "/v1/projects/p/agents/reviewer/placement"),
        ("PUT", "/v1/projects/p/agents/reviewer/account-pool"),
        ("PUT", "/v1/projects/p/agent-types/builder/builder"),
    ),
    "workers": (
        ("GET", "/v1/workers"),
        ("POST", "/v1/workers/pairings"),
        ("POST", "/v1/workers/7/drain"),
        ("DELETE", "/v1/workers/7"),
        ("GET", "/v1/worker-pools"),
        ("POST", "/v1/worker-pools/ci/join-tokens"),
        ("PUT", "/v1/worker-pools/ci/labels"),
        ("GET", "/v1/org/workers"),
        ("PATCH", "/v1/org/workers/7"),
    ),
    "curator": (
        ("PUT", "/v1/projects/p/curator/charter"),
        ("POST", "/v1/projects/p/curator/plans/rollout/build"),
        ("POST", "/v1/projects/p/curator/proposals/1/answer"),
    ),
    "releases": (
        ("POST", "/v1/projects/p/releases/run"),
        ("PUT", "/v1/projects/p/releases/schedule"),
        ("POST", "/v1/projects/p/releases/resume"),
    ),
    "answers": (("POST", "/v1/kg/p/answers"),),
    "runs": (
        ("POST", "/v1/projects/p/runs"),
        ("POST", "/v1/projects/p/plan-runs"),
        ("POST", "/v1/projects/p/author-runs"),
        ("POST", "/v1/projects/p/runs/9/rerun"),
        ("POST", "/v1/projects/p/runs/9/messages"),
        ("POST", "/v1/projects/p/local-runs/abc/inbox"),
        ("POST", "/v1/x/editorial/p/posts/3/run"),
    ),
}
# (method, path) refused whatever the owner opened
FIXED = (
    ("GET", "/v1/projects/p/runs/9/terminal"),
    ("GET", "/v1/projects/p/runs/9/browser"),
    ("POST", "/v1/projects/p/runs/9/takeover"),
    ("POST", "/v1/projects/p/runs/9/handback"),
    ("POST", "/v1/worker/claim"),
    ("GET", "/v1/worker/runs/9/credentials"),
    ("GET", "/v1/projects/p/agents/reviewer/accounts"),
    ("PUT", "/v1/projects/p/agents/reviewer/accounts/claude"),
    ("GET", "/v1/projects/p/agents/reviewer/provider-accounts"),
    ("GET", "/v1/secrets"),
    ("PATCH", "/v1/secrets/claude"),  # a secret's binding names workers and pools, but is no group of workers
    ("GET", "/v1/provider-accounts"),
    ("GET", "/v1/tokens"),
    ("POST", "/v1/auth/logout"),
    ("GET", REACH),
    ("PUT", REACH),
    ("PUT", REACH + "/later"),
    ("PUT", "/v1/projects/p/runs/9/access"),
    ("PUT", "/v1/autonomy/prefs"),
    ("POST", "/v1/autonomy/stops"),
    ("POST", "/v1/org/seats"),
    ("PUT", "/v1/org/ceilings"),
    ("POST", "/v1/org/workersx"),  # a segment, not a string's start: the org chart, not its workers
    ("PUT", "/v1/projects/p/policy"),
    ("PUT", "/v1/admin/projects/p/grants/alice"),
    ("POST", "/v1/projects/p/decisions/1/answer"),
    ("POST", "/v1/projects/p/runs/9/approve"),
    ("POST", "/v1/x/builder/p/proposals/1/answer"),
)


# The rules


@pytest.mark.parametrize("group", REACH_GROUPS)
def test_an_empty_reach_refuses_each_optional_group_and_says_how_to_open_it(group):
    for method, path in OPTIONAL[group]:
        assert refusal(method, path) == refusal(method, path, frozenset()), (method, path)
        message = refusal(method, path)
        assert message is not None, (method, path)
        assert f"(group {group}): the owner does that with their own sign-in, or lets their runs reach it" in message
        assert message.endswith(f"`evo-agents hub autonomy run-reach --allow {group}`"), message


def test_the_hint_names_what_the_owner_opened_already_with_the_group_refused():
    message = refusal("PUT", "/v1/projects/p/curator/charter", frozenset({"workers"}))
    assert message.endswith("`evo-agents hub autonomy run-reach --allow workers,curator`"), message
    message = refusal("GET", "/v1/workers", frozenset({"curator", "agents"}))
    assert message.endswith("`evo-agents hub autonomy run-reach --allow agents,workers,curator`"), message


@pytest.mark.parametrize("group", REACH_GROUPS)
def test_each_group_opened_lets_exactly_its_own_paths_through(group):
    for method, path in OPTIONAL[group]:
        assert refusal(method, path, frozenset({group})) is None, (method, path)
    for other in set(REACH_GROUPS) - {group}:
        for method, path in OPTIONAL[other]:
            message = refusal(method, path, frozenset({group}))
            assert message is not None and f"(group {other})" in message, (group, method, path)
    for method, path in FIXED:
        assert refusal(method, path, frozenset({group})) is not None, (group, method, path)


@pytest.mark.parametrize(("method", "path"), FIXED)
def test_the_fixed_groups_stay_refused_with_all_six_open(method, path):
    message = refusal(method, path, ALL)
    assert message is not None, (method, path)
    assert "lets their runs reach it" not in message, "a fixed group says no way to open it"
    assert message.endswith("the owner does that with their own sign-in")


def test_a_name_outside_the_six_opens_nothing():
    names = frozenset({"secrets", "tokens", "terminal", "run access", "autonomy", "decisions", "grants"})
    for method, path in FIXED:
        assert refusal(method, path, names | ALL) is not None, (method, path)


def test_reading_agents_the_curator_the_releases_and_the_answers_was_open_and_stays_open():
    paths = (
        "/v1/projects/p/agents",
        "/v1/projects/p/agents/reviewer",
        "/v1/projects/p/curator/charter",
        "/v1/projects/p/releases",
        "/v1/projects/p/releases/cycles",
        "/v1/kg/p/answers",
        "/v1/kg/p/answers/1",
        "/v1/kg/p/answers/1/lines",
    )
    for path in paths:
        assert refusal("GET", path) is None and refusal("GET", path, ALL) is None, path
    # the replies of a batch are the route's to refuse (kg_answers.reply): the group answers is the job's asking alone
    assert refusal("POST", "/v1/kg/p/answers/1/replies") is None


def test_reach_order_keeps_each_group_once_in_the_hubs_order():
    assert run_access_rules.reach_order(["curator", "agents", "curator"]) == ("agents", "curator")
    assert run_access_rules.reach_order([]) == ()


# The route and the run's token


def test_the_member_reads_and_sets_their_own_reach_and_each_change_is_audited(client, hub, hub_db):
    shown = client.get(REACH, headers=hub["owner"])
    assert shown.status_code == 200, shown.text
    assert shown.json() == {"login": OWNER, "groups": []}
    written = client.put(REACH, json={"groups": ["curator", "agents", "agents"]}, headers=hub["owner"])
    assert written.status_code == 200, written.text
    assert written.json() == {"login": OWNER, "groups": ["agents", "curator"], "previous": []}
    again = client.put(REACH, json={"groups": ["agents", "curator"]}, headers=hub["owner"])
    assert again.json()["previous"] == ["agents", "curator"]
    cleared = client.put(REACH, json={"groups": []}, headers=hub["owner"])
    assert cleared.json() == {"login": OWNER, "groups": [], "previous": ["agents", "curator"]}
    # another member's reach is theirs: the owner's choice changed nothing of it
    assert client.get(REACH, headers=hub["other"]).json()["groups"] == []
    for body in ({"groups": ["secrets"]}, {"groups": ["agents"], "login": "lumen"}, {}):
        refused = client.put(REACH, json=body, headers=hub["owner"])
        assert refused.status_code == 422, (body, refused.text)
    rows = audit_of(hub_db, "run_access.reach")
    assert [row[:3] for row in rows] == [
        (f"reach {OWNER} none->agents,curator", OWNER, None),
        (f"reach {OWNER} agents,curator->none", OWNER, None),
    ], "a write that changes nothing adds no row"


def test_a_runs_token_reaches_what_its_owner_opened_from_its_next_request(client, hub, hub_db):
    owner_admin(client, hub)
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, token = as_me_run(client, hub, worker)
    run = as_run(token)
    reviewer = f"/v1/projects/{PROJECT}/agents/reviewer"
    charter = f"/v1/projects/{PROJECT}/curator/charter"

    def middleware_refused(response) -> bool:
        return response.status_code == 403 and "never reaches" in response.json()["message"]

    # empty: refused as before
    for method, path in (("GET", "/v1/workers"), ("PATCH", reviewer), ("PUT", charter)):
        response = client.request(method, path, json={}, headers=run)
        assert middleware_refused(response), (method, path, response.text)
        assert "evo-agents hub autonomy run-reach --allow" in response.json()["message"]
    # the run's token neither reads nor widens its reach, its owner's sign-in does
    for method in ("GET", "PUT"):
        response = client.request(method, REACH, json={"groups": list(REACH_GROUPS)}, headers=run)
        assert middleware_refused(response) and "(group run access)" in response.json()["message"], response.text
    opened = client.put(REACH, json={"groups": list(REACH_GROUPS)}, headers=hub["owner"])
    assert opened.status_code == 200, opened.text
    # the same token, no new lease: the next request reads the owner's choice
    listed = client.get("/v1/workers", headers=run)
    assert listed.status_code == 200, listed.text
    assert worker["id"] in {item["id"] for item in listed.json()}
    renamed = client.patch(reviewer, json={"name": "Reviewer Two"}, headers=run)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Reviewer Two", renamed.text
    curated = client.put(charter, json={}, headers=run)  # past the middleware: the route reads the body
    assert curated.status_code == 422, curated.text
    for method, path in (
        ("GET", f"/v1/projects/{PROJECT}/agents/reviewer/accounts"),
        ("GET", f"/v1/projects/{PROJECT}/runs/{run_id}/terminal"),
        ("POST", f"/v1/projects/{PROJECT}/runs/{run_id}/takeover"),
        ("GET", "/v1/secrets"),
        ("GET", "/v1/tokens"),
        ("PUT", REACH),
        ("PUT", "/v1/autonomy/prefs"),
        ("POST", f"/v1/projects/{PROJECT}/decisions/1/answer"),
    ):
        response = client.request(method, path, json={}, headers=run)
        assert middleware_refused(response), (method, path, response.text)
        assert token not in response.text
    # each write of the token is audited as before, the reached ones too
    written = [row[0] for row in audit_of(hub_db, "run_access.write")]
    assert any(f" PATCH {reviewer} " in target for target in written), written
    # cleared: refused again at once
    assert client.put(REACH, json={"groups": []}, headers=hub["owner"]).status_code == 200
    again = client.get("/v1/workers", headers=run)
    assert middleware_refused(again), again.text
    assert client.get("/v1/workers", headers=hub["owner"]).status_code == 200


def test_a_runs_token_asks_an_answer_job_once_its_owner_opened_answers(client, hub, hub_db):
    """A run that runs as its owner and must answer a bench's questions asks the job itself once its owner opened the
    run reach answers; without it the run's token is refused and told how, and no job is kept. The job is the owner's,
    as a file they sent, so its batches go out as theirs; the reach opens the asking alone."""
    worker = worker_of(client, hub["owner"], "mac-mini")
    _, token = as_me_run(client, hub, worker)
    run = as_run(token)
    answers = f"/v1/kg/{PROJECT}/answers"
    body = {"questions": [{"id": "1", "question": "Where is the ledger kept?"}]}
    jobs = select(func.count()).select_from(tables.kg_answer_jobs)

    refused = client.post(answers, json=body, headers=run)
    assert refused.status_code == 403, refused.text
    message = refused.json()["message"]
    assert "(group answers)" in message and message.endswith("run-reach --allow answers`"), message
    assert sql(hub_db, jobs) == [(0,)]
    assert client.get(answers, headers=run).status_code == 200, "reading the jobs needs no reach"
    # the other groups open nothing of it
    others = [group for group in REACH_GROUPS if group != "answers"]
    assert client.put(REACH, json={"groups": others}, headers=hub["owner"]).status_code == 200
    assert client.post(answers, json=body, headers=run).status_code == 403

    opened = client.put(REACH, json={"groups": ["answers"]}, headers=hub["owner"])
    assert opened.status_code == 200, opened.text
    asked = client.post(answers, json=body, headers=run)
    assert asked.status_code == 201, asked.text
    assert (asked.json()["created_by"], asked.json()["questions"]) == (OWNER, 1)
    # the reach opens the asking alone: the token still starts no run of its own
    plan_run = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={}, headers=run)
    assert plan_run.status_code == 403 and "(group runs)" in plan_run.json()["message"], plan_run.text
    written = [row[0] for row in audit_of(hub_db, "run_access.write")]
    assert any(f" POST {answers} 201" in target for target in written), written
    assert not any(token in target for target in written)

    assert client.put(REACH, json={"groups": []}, headers=hub["owner"]).status_code == 200
    again = client.post(answers, json={"questions": [{"id": "2", "question": "Who signs?"}]}, headers=run)
    assert again.status_code == 403 and "(group answers)" in again.json()["message"], again.text
    assert sql(hub_db, jobs) == [(1,)]


def test_a_runs_token_starts_runs_and_hands_agents_work_once_its_owner_opened_runs(client, hub, hub_db):
    """A run that runs as its owner and must drive agents (the demo of an agent system) starts runs and mentions agents
    in a chat once its owner opened the run reach runs; without it each way is refused and says how, and nothing is
    queued. A run it starts never runs as the owner, so the owner's account goes no further than the run let."""
    owner_admin(client, hub)
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4, pool=PROJECT)  # a node of the default agent's
    _, token = as_me_run(client, hub, worker)
    run = as_run(token)
    dispatch = f"/v1/projects/{PROJECT}/runs"
    body = {"plan_id": PLAN, "steps": [STEPS["evo-agents"]]}
    r = tables.runs
    count = select(func.count()).select_from(r)
    before = sql(hub_db, count)
    made = client.post("/v1/chats", json={"title": "Demo", "people": [], "agents": [PROJECT]}, headers=run)
    assert made.status_code == 201, made.text
    messages = f"/v1/chats/{made.json()['id']}/messages"

    refused = client.post(dispatch, json=body, headers=run)
    assert refused.status_code == 403, refused.text
    message = refused.json()["message"]
    assert "(group runs)" in message and message.endswith("run-reach --allow runs`"), message
    assert client.post(messages, json={"body": f"@{PROJECT} read the diff"}, headers=run).status_code == 201
    # the other groups open nothing of it
    others = [group for group in REACH_GROUPS if group != "runs"]
    assert client.put(REACH, json={"groups": others}, headers=hub["owner"]).status_code == 200
    assert client.post(dispatch, json=body, headers=run).status_code == 403
    assert client.post(messages, json={"body": f"@{PROJECT} read it again"}, headers=run).status_code == 201
    assert sql(hub_db, count) == before, "no run, no chat run while runs is closed"
    listed = client.get(messages, params={"limit": 50}, headers=hub["owner"]).json()["messages"]
    said = [m["body"] for m in listed if run_access_rules.NEW_RUN in m["body"]]
    assert said and all(f"run-reach --allow {','.join(others)},runs`" in text for text in said[-1:]), said

    opened = client.put(REACH, json={"groups": ["runs"]}, headers=hub["owner"])
    assert opened.status_code == 200, opened.text
    started = client.post(dispatch, json=body, headers=run)
    assert started.status_code == 201, started.text
    new_id = started.json()[0]["id"]
    assert sql(hub_db, select(r.c.dispatched_by, r.c.as_owner).where(r.c.id == new_id)) == [
        (sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0], False)
    ]
    # the run it starts never runs as the owner: the reach opens no onward account
    onward = client.post(dispatch, json={**body, "as_me": True}, headers=run)
    assert onward.status_code == 403 and onward.json()["message"] == run_access.ONWARD, onward.text
    handed = client.post(messages, json={"body": f"@{PROJECT} read the diff now"}, headers=run)
    assert handed.status_code == 201, handed.text
    chat_runs = sql(hub_db, select(r.c.id, r.c.hop, r.c.as_owner).where(r.c.kind == "chat"))
    assert len(chat_runs) == 1 and chat_runs[0][1:] == (1, False), chat_runs
    written = [row[0] for row in audit_of(hub_db, "run_access.write")]
    assert any(f" POST {dispatch} 201" in target for target in written), written
    assert not any(token in target for target in written)

    assert client.put(REACH, json={"groups": []}, headers=hub["owner"]).status_code == 200
    again = client.post(dispatch, json=body, headers=run)
    assert again.status_code == 403 and "(group runs)" in again.json()["message"], again.text


def test_the_store_refuses_the_agent_of_a_run_behind_the_middleware(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, _ = as_me_run(client, hub, worker)
    run = run_principal(hub_db, run_id)

    async def attempt(call):
        async with client.app.state.engine.begin() as conn:
            try:
                await call(conn)
            except HTTPException as exc:
                return exc.status_code, exc.detail
        return None

    for call in (
        lambda conn: autonomy_store.run_reach(conn, run),
        lambda conn: autonomy_store.write_run_reach(conn, run, RunReachSet(groups=["workers"])),
    ):
        status, detail = client.portal.call(attempt, call)
        assert status == 403 and "agent of a run" in detail
    assert client.get(REACH, headers=hub["owner"]).json()["groups"] == []


# The command line


def test_allow_parses_a_comma_separated_list_into_the_hubs_order():
    assert reach_groups("curator,agents") == ["agents", "curator"]
    assert reach_groups(" workers , workers ,") == ["workers"]
    assert reach_groups("runs,answers,releases,agents,workers,curator") == list(REACH_GROUPS)
    for wrong, said in (("", "names one or more"), (",", "names one or more"), ("agents,secrets", "'secrets'")):
        with pytest.raises(Usage) as refused:
            reach_groups(wrong)
        assert said in str(refused.value), wrong


def test_run_reach_from_the_command_line(client, hub, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub_client, "_send", through(client))
    monkeypatch.setenv("HOME", str(tmp_path / "laptop"))
    saved_login(tmp_path / "laptop" / ".evo" / "hub", OWNER, token_of(hub["owner"]))
    run_reach = ("hub", "autonomy", "run-reach")

    shown = command(monkeypatch, capsys, {}, *run_reach)
    assert shown.code == 0, shown.err
    assert shown.out.startswith("Your runs that run as you also reach: none.\n")
    allowed = command(monkeypatch, capsys, {}, *run_reach, "--allow", "curator,workers")
    assert allowed.code == 0, allowed.err
    assert allowed.out == "Your runs that run as you also reach: workers, curator (was none).\n"
    as_json = command(monkeypatch, capsys, {}, *run_reach, "--json")
    assert json.loads(as_json.out) == {"login": OWNER, "groups": ["workers", "curator"]}
    assert_json_keys("hub autonomy run-reach", json.loads(as_json.out))
    cleared = command(monkeypatch, capsys, {}, *run_reach, "--none", "--json")
    assert cleared.code == 0, cleared.err
    assert json.loads(cleared.out) == {"login": OWNER, "groups": [], "previous": ["workers", "curator"]}
    assert_json_keys("hub autonomy run-reach", json.loads(cleared.out), "--none")

    wrong = command(monkeypatch, capsys, {}, *run_reach, "--allow", "agents,tokens")
    assert wrong.code == 2 and "no group 'tokens'" in wrong.err
    with pytest.raises(SystemExit) as both:
        command(monkeypatch, capsys, {}, *run_reach, "--allow", "agents", "--none")
    assert both.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert client.get(REACH, headers=hub["owner"]).json()["groups"] == []
