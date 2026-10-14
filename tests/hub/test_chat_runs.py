"""Chat runs on the hub (``evo_agents.hub.chat_runs``, ``evo_agents.hub.server.chat_runs`` and ``chat_agents``), as
criteria a8 and a9 of the plan agent-profiles-group-chat name them.

a8: a mention of an agent makes one run of kind chat of its profile, on its project, dispatched by the person who
mentioned it, pinned to a worker of that person the profile allows (online, a free slot, a daemon that runs chat runs,
the one with a checkout of more of the project's repos first); no such worker, and the hub answers in the thread why,
with no run; at most three runs a message. The claim hands a prompt with a bounded snapshot of the chat. The agent's
reply at the end of its turn is a message of the agent in the thread; the run's moves put the agent's reaction on the
message that started it, by rule. A reply in the thread goes to the running run's inbox, or wakes the session that
ended with a run that goes on in it, on its worker and branch; a person writing to another's session starts their
own. The run's branch chat/<run> gets a draft pull request the hub opens with the workers' App, linked in the thread;
no run merges.

a9: the agent of a run (chat, step, plan or author) posts, reacts, creates a chat with the person at the origin of the
chain (who is told), and mentions another agent: a run for that same person, one hop further, never to itself, at most
three hops, within the chat's budget a day, which its creator changes; a refusal is answered and said in the thread.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import chat_runs, chats, runs, tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_runs import PROJECT, add_worker, claim, expire, moved, recover, report, set_run, worker_clock

app_key = credentials_api.app_key  # a fixture
MINE = credentials_api.MINE
OWNER, OTHER, STRANGER = credentials_api.OWNER, credentials_api.OTHER, "stranger"
CHECKOUTS = credentials_api.CHECKOUTS  # evo-agents, lumen-identity, lumen-kb-docs and notes
ONE_REPO = {f"{PROJECT}/evo-agents": {"path": "/src/evo-agents", "branch": "main"}}
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
EYES, CHECK, CROSS = chats.EYES, chats.CHECK, chats.CROSS
WORKERS_APP = {"contents": "write", "metadata": "read", "pull_requests": "write"}


@pytest.fixture
def client(hub_db, tmp_path, github, app_key):  # noqa: F811
    config = credentials_api.hub_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project of ``test_credentials_api`` (evo-agents on GitHub and three more repos), not restricted, owner its
    admin and someone-else a writer, stranger signed in without a grant; the agents @reviewer and @fixer of the
    project; the headers of each."""
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, credentials_api.ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, credentials_api.OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, credentials_api.OTHER_ID)["token"]),
        "stranger": bearer(live.sign_in(client, github, STRANGER, 503)["token"]),
    }
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": OPEN_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=headers["admin"]).status_code == 200
    for login, grant in ((OWNER, ADMIN_GRANT), (OTHER, credentials_api.WRITER)):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    for handle in ("reviewer", "fixer"):
        agent(client, headers["owner"], handle)
    return headers


def agent(client, headers, handle: str, **body) -> dict:
    response = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": handle, "name": handle.title(), **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def chat_worker(client, headers, name: str, *, checkouts=CHECKOUTS, kinds=runs.RUN_KINDS, slots: int = 1) -> dict:
    """A worker of ``headers``' member whose daemon says it runs ``kinds`` and applies agent profiles."""
    worker = add_worker(client, headers, name, slots=slots, checkouts=checkouts)
    beat(client, worker, kinds=kinds)
    return worker


def beat(client, worker: dict, held=(), kinds=runs.RUN_KINDS) -> dict:
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": list(held),
        "run_kinds": list(kinds),
        "features": list(runs.WORKER_FEATURES),
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def created(client, headers, title: str = "Release talk", people=(), agents=("reviewer",)) -> dict:
    body = {"title": title, "people": list(people), "agents": list(agents)}
    response = client.post("/v1/chats", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def sent(client, headers, chat_id: int, text: str, reply_to: int | None = None) -> dict:
    payload = {"body": text} | ({"reply_to": reply_to} if reply_to else {})
    response = client.post(f"/v1/chats/{chat_id}/messages", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def thread(client, headers, chat_id: int, root: int) -> list[dict]:
    response = client.get(f"/v1/chats/{chat_id}/messages", params={"thread": root}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def message(client, headers, chat_id: int, message_id: int) -> dict:
    response = client.get(f"/v1/chats/{chat_id}/messages", params={"limit": 200}, headers=headers)
    return next(item for item in response.json()["messages"] if item["id"] == message_id)


def reactions(client, headers, chat_id: int, message_id: int) -> list[tuple]:
    return [(r["emoji"], r["agents"]) for r in message(client, headers, chat_id, message_id)["reactions"]]


def run_of(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def chat_runs_of(db) -> list[tuple]:
    r = tables.runs
    query = select(r.c.id, r.c.state, r.c.resume_of_run_id, r.c.hop, r.c.dispatched_by).where(r.c.kind == "chat")
    return sql(db, query.order_by(r.c.id))


def in_chats(client, worker: dict, run_id: int, route: str, body: dict):
    return client.post(f"/v1/worker/runs/{run_id}/{route}", json=body, headers=worker["headers"])


def reply(client, worker: dict, run_id: int, text: str):
    """The worker posts the agent's last message of a turn (POST .../runs/{id}/chat)."""
    response = client.post(f"/v1/worker/runs/{run_id}/chat", json={"text": text}, headers=worker["headers"])
    assert response.status_code == 201, response.text
    return response.json()


def started(client, hub, worker: dict, chat: dict, text: str = "@reviewer please fix the flaky wait test") -> tuple:
    """The owner's mention of @reviewer in ``chat``, its run claimed by ``worker`` and running: (message, spec)."""
    mention = sent(client, hub["owner"], chat["id"], text)
    spec = claim(client, worker)
    assert spec is not None and spec["kind"] == "chat", spec
    moved(client, worker, spec["id"], "running", session_id="sess-1")
    return mention, spec


def ended(client, worker: dict, run_id: int, **body) -> dict:
    return moved(client, worker, run_id, "verifying", "done", **body)


# A mention makes a run


def test_a_chat_mention_makes_one_run_on_the_senders_worker_with_the_most_repos(client, hub, hub_db):
    small = chat_worker(client, hub["owner"], "mac-mini", checkouts=ONE_REPO)
    large = chat_worker(client, hub["owner"], "studio")
    chat_worker(client, hub["other"], "theirs")
    chat = created(client, hub["owner"], people=[OTHER])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer please fix the flaky wait test")
    ((made, state, resume_of, hop, by),) = chat_runs_of(hub_db)
    assert (state, resume_of, hop) == ("queued", None, 1)
    assert [(m["name"], m["via"], m["run_id"], m["refusal"]) for m in mention["mentions"]] == [
        ("reviewer", "handle", made, None)
    ]
    assert [item["id"] for item in thread(client, hub["owner"], chat["id"], mention["id"])] == [mention["id"]]
    run = run_of(client, hub["other"], made)
    assert (run["kind"], run["plan_id"], run["dispatched_by"], run["profile"], run["pinned_worker_id"]) == (
        "chat",
        "",
        OWNER,
        "reviewer",
        large["id"],
    )
    assert (run["chat_id"], run["chat_message_id"], run["hop"], run["branch"]) == (
        chat["id"],
        mention["id"],
        1,
        f"chat/{made}",
    )
    assert [entry["repo"] for entry in run["repos"]] == ["evo-agents", "lumen-identity", "lumen-kb-docs", "notes"]
    assert {entry["branch"] for entry in run["repos"]} == {f"chat/{made}"}
    assert run["budget"] == {"max_usd": chat_runs.DEFAULT_BUDGET_USD, "max_turns": None, "max_seconds": 1800}
    assert run["title"] == "@reviewer in Release talk: @reviewer please fix the flaky wait test"
    listed = client.get(f"/v1/projects/{PROJECT}/runs", headers=hub["other"]).json()["runs"]
    assert [(item["id"], item["kind"], item["hop"]) for item in listed] == [(made, "chat", 1)]
    # pinned to the owner's studio: neither their other worker nor someone else's takes it
    assert claim(client, small) is None
    assert claim(client, large)["id"] == made


def test_a_chat_mention_without_a_fit_worker_says_why_in_the_thread_and_makes_no_run(client, hub, hub_db):
    old = chat_worker(client, hub["owner"], "old-daemon", kinds=["step", "plan", "review", "judge", "author"])
    asleep = chat_worker(client, hub["owner"], "asleep")
    worker_clock(hub_db, asleep, last_heartbeat_at=runs.OFFLINE_AFTER_SECONDS + 60)
    chat = created(client, hub["owner"])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer look at the diff")
    assert chat_runs_of(hub_db) == []
    (refusal,) = [m["refusal"] for m in mention["mentions"]]
    assert refusal.startswith(f"@reviewer: no worker of {OWNER} can take a run of @reviewer now: ")
    assert "old-daemon runs evo-agents" in refusal and "which takes no chat run" in refusal
    assert "asleep is offline" in refusal
    answer = thread(client, hub["owner"], chat["id"], mention["id"])[-1]
    assert (answer["author"]["kind"], answer["body"]) == ("hub", refusal)
    # a worker the profile leaves out does not take it either
    agent(client, hub["owner"], "narrow", permissions={"worker": {"mode": "allowlist", "allow": ["studio"]}})
    added = client.put(f"/v1/chats/{chat['id']}/agents/narrow", headers=hub["owner"])
    assert added.status_code == 200, added.text
    beat(client, old)  # now it runs chat runs
    narrowed = sent(client, hub["owner"], chat["id"], "@narrow and @reviewer")
    assert [m["refusal"] for m in narrowed["mentions"]][0].endswith(
        "old-daemon is not one @narrow allows; asleep is offline"
    )
    assert [m["run_id"] is not None for m in narrowed["mentions"]] == [False, True]
    # someone with no worker for the project at all is told that
    other = sent(client, hub["owner"], chat["id"], "@reviewer again")  # the owner's one worker is busy now
    assert "old-daemon has no free slot" in other["mentions"][0]["refusal"]


def test_a_chat_message_hands_work_to_three_agents_at_most(client, hub, hub_db):
    chat_worker(client, hub["owner"], "studio", slots=4)
    for handle in ("third", "fourth"):
        agent(client, hub["owner"], handle)
    chat = created(client, hub["owner"], agents=["reviewer", "fixer", "third", "fourth"])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer @fixer @third @fourth: one each")
    assert [m["run_id"] is not None for m in mention["mentions"]] == [True, True, True, False]
    assert mention["mentions"][3]["refusal"] == "@fourth: a message hands work to at most 3 agents"
    assert len(chat_runs_of(hub_db)) == 3


# The run, its prompt, its reply and its reactions


def test_a_chat_run_claim_has_the_bounded_snapshot_and_its_moves_react_by_rule(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer", "fixer"])
    sent(client, hub["other"], chat["id"], "The nightly build is red since Tuesday.")
    root = sent(client, hub["owner"], chat["id"], "The wait helper sleeps 5 seconds `@reviewer` in tests.")
    sent(client, hub["other"], chat["id"], "It hits tests/test_wait.py the most.", reply_to=root["id"])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer replace the sleep with a poll, please", root["id"])
    spec = claim(client, worker)
    prompt = spec["prompt"]
    assert prompt.startswith(f"You are @reviewer (Reviewer), an agent of project {PROJECT} on the evo-agents hub")
    assert f"Work for {OWNER}" in prompt and f"the branch chat/{spec['id']}" in prompt
    assert "## The chat before the thread" in prompt and "The nightly build is red since Tuesday." in prompt
    assert "## The thread" in prompt and "It hits tests/test_wait.py the most." in prompt
    assert prompt.index("The wait helper sleeps") < prompt.index("# The message to answer")
    assert prompt.rstrip().endswith("@reviewer replace the sleep with a poll, please")
    assert "budget of $5.00 for 24 hours" in prompt and "at hop 2 of at most 3" in prompt
    assert (spec["branch"], spec["budget"]["max_usd"], spec["profile"]["handle"]) == (
        f"chat/{spec['id']}",
        5.0,
        "reviewer",
    )
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(EYES, ["reviewer"])]

    moved(client, worker, spec["id"], "running", session_id="sess-1")
    reply(client, worker, spec["id"], "Replaced the sleep with wait_for. @someone-else, please look. @fixer too.")
    said = thread(client, hub["owner"], chat["id"], root["id"])[-1]
    assert (said["author"]["kind"], said["author"]["handle"], said["run_id"]) == ("agent", "reviewer", spec["id"])
    assert [(m["name"], m["refusal"]) for m in said["mentions"]] == [
        (OTHER, None),
        ("fixer", "an agent's @ in a message hands no work: it uses `evo-agents worker chat mention`"),
    ]
    assert len(chat_runs_of(hub_db)) == 1  # the @fixer of a reply hands it nothing
    notices = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=hub["other"]).json()
    assert any(n["title"] == "@reviewer mentioned you in chat Release talk" for n in notices["notifications"])
    ended(client, worker, spec["id"], summary="Polled instead of sleeping.")
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(CHECK, ["reviewer"])]


def test_a_chat_run_that_fails_says_why_in_the_thread_with_a_cross(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    moved(
        client,
        worker,
        spec["id"],
        "failed",
        error="claude-code stopped at the run's time cap",
        failure_cause="time_cap",
    )
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(CROSS, ["reviewer"])]
    answer = thread(client, hub["owner"], chat["id"], mention["id"])[-1]
    assert answer["author"]["kind"] == "hub"
    assert answer["body"] == f"Run #{spec['id']} of @reviewer failed: claude-code stopped at the run's time cap"
    rerun = client.post(f"/v1/projects/{PROJECT}/runs/{spec['id']}/rerun", headers=hub["owner"])
    assert rerun.status_code == 409 and "mention its agent in the chat again" in rerun.json()["message"]


# Replies in the thread


def test_a_chat_reply_goes_to_the_running_inbox_and_then_resumes_the_session(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, worker, chat)
    while_running = sent(client, hub["owner"], chat["id"], "@reviewer also look at retries", mention["id"])
    assert while_running["mentions"][0]["run_id"] == spec["id"]
    control = beat(client, worker, held=[spec["id"]])["runs"][0]
    assert control["inbox"] == 1
    inbox = client.post(f"/v1/worker/runs/{spec['id']}/inbox", json={}, headers=worker["headers"]).json()["messages"]
    assert [item["text"] for item in inbox] == [
        f"{OWNER} wrote in the thread (message #{while_running['id']}):\n@reviewer also look at retries"
    ]
    client.post(f"/v1/worker/runs/{spec['id']}/inbox", json={"ack": inbox[0]["id"]}, headers=worker["headers"])
    ended(client, worker, spec["id"], usage={"total_cost_usd": 0.4})
    assert len(chat_runs_of(hub_db)) == 1, "a message the run took wakes nothing"

    # once the run is done, a reply in the thread goes on in its session, as a new run on the same worker
    later = sent(client, hub["owner"], chat["id"], "@reviewer and the docs?", mention["id"])
    (_, (next_id, state, resume_of, hop, _)) = chat_runs_of(hub_db)
    assert (later["mentions"][0]["run_id"], state, resume_of, hop) == (next_id, "queued", spec["id"], 1)
    run = run_of(client, hub["owner"], next_id)
    assert (run["pinned_worker_id"], run["session_id"], run["branch"], run["chat_message_id"]) == (
        worker["id"],
        "sess-1",
        f"chat/{spec['id']}",
        later["id"],
    )
    assert run["budget"]["max_usd"] == pytest.approx(5.0 - 0.4 + 0.4)  # what is left, plus the session's cost
    goes_on = claim(client, worker)
    assert (goes_on["id"], goes_on["resume_of_run_id"], goes_on["session_id"]) == (next_id, spec["id"], "sess-1")
    assert goes_on["budget"]["spent_usd"] == pytest.approx(0.4)
    held = client.post(f"/v1/worker/runs/{next_id}/inbox", json={}, headers=worker["headers"]).json()["messages"]
    assert [item["text"].splitlines()[-1] for item in held] == ["@reviewer and the docs?"]


def test_a_chat_run_done_with_messages_untaken_resumes_its_session_with_them(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    sent(client, hub["owner"], chat["id"], "one more thing", mention["id"])  # one person: no @ needed in the thread
    ended(client, worker, spec["id"])
    (_, (next_id, state, resume_of, _, _)) = chat_runs_of(hub_db)
    assert (state, resume_of) == ("queued", spec["id"])
    held = sql(hub_db, select(tables.run_inbox.c.run_id).where(tables.run_inbox.c.delivered_at.is_(None)))
    assert held == [(next_id,)]


def test_a_chat_reply_without_a_mention_wakes_the_session_in_a_chat_of_one_person_only(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio", slots=2)
    alone = created(client, hub["owner"], title="Just me")
    mention, spec = started(client, hub, worker, alone)
    ended(client, worker, spec["id"])
    woke = sent(client, hub["owner"], alone["id"], "and the docs?", mention["id"])
    assert [(m["name"], m["via"]) for m in woke["mentions"]] == [("reviewer", "thread")]
    assert len(chat_runs_of(hub_db)) == 2
    # with two people in the chat, a reply wakes nobody without an @
    pair = created(client, hub["owner"], title="The two of us", people=[OTHER])
    other_mention = sent(client, hub["owner"], pair["id"], "@reviewer check the logs")
    quiet = sent(client, hub["owner"], pair["id"], "thanks", other_mention["id"])
    assert quiet["mentions"] == [] and len(chat_runs_of(hub_db)) == 3


def test_another_person_mentioning_the_agent_in_the_thread_starts_a_chat_session_of_their_own(client, hub, hub_db):
    mine = chat_worker(client, hub["owner"], "studio")
    theirs = chat_worker(client, hub["other"], "laptop")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, mine, chat)
    ended(client, mine, spec["id"])
    second = sent(client, hub["other"], chat["id"], "@reviewer do it my way", mention["id"])
    run = run_of(client, hub["other"], second["mentions"][0]["run_id"])
    assert (run["dispatched_by"], run["pinned_worker_id"], run["resume_of_run_id"]) == (OTHER, theirs["id"], None)
    assert run["branch"] == f"chat/{run['id']}"


# The agent in chats: post, react, create, mention


def test_an_agent_mention_hands_work_one_hop_further_for_the_origin(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio", slots=3)
    chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
    mention, spec = started(client, hub, worker, chat)
    handed = in_chats(client, worker, spec["id"], "chat/mentions", {"handle": "fixer", "body": "fix the retry loop"})
    assert handed.status_code == 201, handed.text
    answer = handed.json()
    assert (answer["refusal"], answer["message"]["body"]) == (None, "@fixer fix the retry loop")
    assert (answer["message"]["author"]["handle"], answer["message"]["thread_root_id"]) == ("reviewer", mention["id"])
    second = run_of(client, hub["owner"], answer["run_id"])
    assert (second["profile"], second["dispatched_by"], second["hop"], second["pinned_worker_id"]) == (
        "fixer",
        OWNER,
        2,
        worker["id"],
    )
    # never itself, and never past the third hop
    itself = in_chats(client, worker, spec["id"], "chat/mentions", {"handle": "reviewer", "body": "me again"}).json()
    assert itself["run_id"] is None and itself["refusal"] == chat_runs.hop_problem(2, True)
    set_run(hub_db, spec["id"], hop=3)
    past = in_chats(client, worker, spec["id"], "chat/mentions", {"handle": "fixer", "body": "once more"}).json()
    assert past["run_id"] is None and "hop 4 from the person's message" in past["refusal"]
    said = [
        item["body"]
        for item in thread(client, hub["owner"], chat["id"], mention["id"])
        if item["author"]["kind"] == "hub"
    ]
    assert said[-1] == f"@fixer: {past['refusal']}"
    unknown = in_chats(client, worker, spec["id"], "chat/mentions", {"handle": "nobody", "body": "hello"}).json()
    assert unknown["refusal"] == "no agent @nobody in this chat"


def test_chat_budget_refuses_new_runs_once_spent_and_only_its_creator_changes_it(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio", slots=3)
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer", "fixer"])
    assert chat["budget_usd"] == chat_runs.DEFAULT_BUDGET_USD
    mention, spec = started(client, hub, worker, chat)
    ended(client, worker, spec["id"], usage={"total_cost_usd": 5.2})
    spent = sent(client, hub["owner"], chat["id"], "@fixer now you")
    assert spent["mentions"][0]["run_id"] is None
    assert spent["mentions"][0]["refusal"] == (
        "@fixer: the chat's budget of $5.00 for 24 hours is spent: its agents' runs cost $5.20 in that time; the "
        "chat's creator may raise it"
    )
    refused = client.patch(f"/v1/chats/{chat['id']}", json={"budget_usd": 20}, headers=hub["other"])
    assert refused.status_code == 403
    assert client.patch(f"/v1/chats/{chat['id']}", json={"budget_usd": 0}, headers=hub["owner"]).status_code == 422
    raised = client.patch(f"/v1/chats/{chat['id']}", json={"budget_usd": 20}, headers=hub["owner"])
    assert raised.status_code == 200 and raised.json()["budget_usd"] == 20
    again = sent(client, hub["owner"], chat["id"], "@fixer now you")
    run = run_of(client, hub["owner"], again["mentions"][0]["run_id"])
    assert run["budget"]["max_usd"] == pytest.approx(14.8)
    rows = sql(
        hub_db, select(tables.audit.c.action, tables.audit.c.target).where(tables.audit.c.action == "chat.budget")
    )
    assert rows == [("chat.budget", f"chat:{chat['id']} budget_usd=20")]


def test_an_agent_posts_reacts_and_create_makes_a_chat_with_the_origin_who_is_told(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, worker, chat)
    posted = in_chats(client, worker, spec["id"], "chat/messages", {"body": "Working on it, @someone-else."})
    assert posted.status_code == 201, posted.text
    assert (posted.json()["thread_root_id"], posted.json()["author"]["handle"]) == (mention["id"], "reviewer")
    reacted = in_chats(
        client, worker, spec["id"], "chat/reactions", {"message_id": mention["id"], "emoji": "\U0001f44d"}
    )
    assert reacted.status_code == 200, reacted.text
    assert ("\U0001f44d", ["reviewer"]) in reactions(client, hub["owner"], chat["id"], mention["id"])

    made = in_chats(client, worker, spec["id"], "chats", {"title": "Retry design", "people": [OTHER]})
    assert made.status_code == 201, made.text
    new_chat = made.json()
    assert [p["login"] for p in new_chat["people"]] == [OWNER, OTHER]
    assert [a["handle"] for a in new_chat["agents"]] == ["reviewer"]
    assert new_chat["created_by"] == OWNER
    for who in ("owner", "other"):
        notices = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=hub[who]).json()[
            "notifications"
        ]
        assert any(
            n["notice_kind"] == "chat_added" and n["title"] == "@reviewer added you to Retry design" for n in notices
        )
    members = tables.chat_members
    added = sql(hub_db, select(members.c.added_by_run_id).where(members.c.chat_id == new_chat["id"]))
    assert added == [(spec["id"],)] * 3
    nobody = in_chats(client, worker, spec["id"], "chats", {"title": "Leak", "people": [STRANGER]})
    assert nobody.status_code == 409 and f"{STRANGER} (no grant on project {PROJECT})" in nobody.json()["message"]
    there = in_chats(client, worker, spec["id"], "chat/messages", {"body": "Here.", "chat_id": new_chat["id"]})
    assert there.status_code == 201 and there.json()["thread_root_id"] is None


def test_the_chat_commands_of_a_step_run_name_a_chat_and_create_one_for_its_dispatcher(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    plan = {"body": credentials_api.plan_body()}
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{credentials_api.PLAN}", json=plan, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text
    body = {"plan_id": credentials_api.PLAN, "steps": [1]}
    assert client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"]).status_code == 201
    spec = claim(client, worker)
    assert spec["kind"] == "step"
    nowhere = in_chats(client, worker, spec["id"], "chat/messages", {"body": "hello"})
    assert nowhere.status_code == 422 and "name one with chat_id" in nowhere.json()["message"]
    made = in_chats(client, worker, spec["id"], "chats", {"title": "Step 1 notes"}).json()
    assert [a["handle"] for a in made["agents"]] == [PROJECT] and [p["login"] for p in made["people"]] == [OWNER]
    posted = in_chats(client, worker, spec["id"], "chat/messages", {"body": "Done with step 1.", "chat_id": made["id"]})
    assert posted.status_code == 201 and posted.json()["author"]["handle"] == PROJECT
    other = chat_worker(client, hub["other"], "laptop")
    assert in_chats(client, other, spec["id"], "chat/messages", {"body": "x", "chat_id": made["id"]}).status_code == 404
    # a chat the agent is in but its run's dispatcher is not: the agent talks there for nobody
    theirs = created(client, hub["other"], title="Their chat", agents=[PROJECT])
    away = in_chats(client, worker, spec["id"], "chat/messages", {"body": "hi", "chat_id": theirs["id"]})
    assert (
        away.status_code == 404 and f"{OWNER}, who dispatched run {spec['id']}, is in no chat" in away.json()["message"]
    )


# The branch and its draft pull request


def test_a_chat_run_branch_gets_a_draft_pull_request_linked_in_the_thread(client, hub, hub_db, github):
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    branch = f"chat/{spec['id']}"
    github.push(MINE, "evo-agents", branch, "1" * 40)
    body = {"repo": "evo-agents", "branch": branch, "head": "1" * 40}
    opened = in_chats(client, worker, spec["id"], "chat/pulls", body)
    assert opened.status_code == 201, opened.text
    pull = opened.json()
    assert (pull["number"], pull["opened"]) == (1, True)
    assert pull["url"] == f"https://github.com/{MINE}/evo-agents/pull/1"
    made = github.pull_of(MINE, "evo-agents", branch)
    assert (made.base, made.draft, made.merged) == ("main", True, False)
    said = message(client, hub["owner"], chat["id"], pull["message_id"])
    assert (said["author"]["handle"], said["thread_root_id"], said["body"]) == (
        "reviewer",
        mention["id"],
        f"Opened a draft pull request of evo-agents from {branch}: {pull['url']}",
    )
    again = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "head": "2" * 40}).json()
    assert (again["number"], again["opened"]) == (1, False)
    assert message(client, hub["owner"], chat["id"], again["message_id"])["body"].startswith("Pushed more commits")
    # a repo off GitHub gets the message without a pull request, and the run's branch alone is taken
    elsewhere = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "repo": "lumen-kb-docs"}).json()
    assert elsewhere["url"] is None and elsewhere["number"] is None
    wrong = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "branch": "main"})
    assert wrong.status_code == 422


def test_a_lost_chat_run_is_tried_again_with_its_chat_columns(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    expire(hub_db, spec["id"])
    recover(client)
    r = tables.runs
    retry = sql(
        hub_db,
        select(r.c.chat_id, r.c.chat_message_id, r.c.hop, r.c.branch, r.c.parent_run_id).where(
            r.c.id != spec["id"], r.c.kind == "chat"
        ),
    )
    assert retry == [(chat["id"], mention["id"], 1, f"chat/{spec['id']}", spec["id"])]
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(EYES, ["reviewer"])]  # the eyes stay
    assert report(client, worker, spec["id"], "running").status_code == 404
