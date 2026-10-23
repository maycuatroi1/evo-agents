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
three hops, with no cost cap of the chat; a refusal is answered and said in the thread.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from evo_agents.hub import autonomy_card, chat_runs, chats, runs, tables
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
# the features of a daemon whose chat run starts with every repo it has a checkout of, as one before LAZY_CHAT_REPOS
EAGER = tuple(feature for feature in runs.WORKER_FEATURES if feature != runs.LAZY_CHAT_REPOS)
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


def chat_worker(
    client, headers, name: str, *, checkouts=CHECKOUTS, kinds=runs.RUN_KINDS, slots: int = 1, features=EAGER
) -> dict:
    """A worker of ``headers``' member whose daemon says it runs ``kinds`` and does ``features``: it applies agent
    profiles, and its chat runs start with every repo it has a checkout of unless they name LAZY_CHAT_REPOS."""
    worker = add_worker(client, headers, name, slots=slots, checkouts=checkouts)
    worker["features"] = list(features)
    beat(client, worker, kinds=kinds)
    return worker


def beat(client, worker: dict, held=(), kinds=runs.RUN_KINDS) -> dict:
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": list(held),
        "run_kinds": list(kinds),
        "features": worker.get("features", list(EAGER)),
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
    assert run["budget"] == {"max_usd": None, "max_turns": None, "max_seconds": 1800}
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
    prompt = autonomy_card.without_card(spec["prompt"])  # the card opens it (autonomy-levels)
    assert prompt.startswith(f"You are @reviewer (Reviewer), an agent of project {PROJECT} on the evo-agents hub")
    assert f"Work for {OWNER}" in prompt and f"the branch chat/{spec['id']}" in prompt
    assert "## The chat before the thread" in prompt and "The nightly build is red since Tuesday." in prompt
    assert "## The thread" in prompt and "It hits tests/test_wait.py the most." in prompt
    assert prompt.index("The wait helper sleeps") < prompt.index("# The message to answer")
    assert prompt.rstrip().endswith("@reviewer replace the sleep with a poll, please")
    assert "at hop 2 of at most 3" in prompt and "budget" not in prompt
    assert (spec["branch"], spec["budget"]["max_usd"], spec["profile"]["handle"]) == (
        f"chat/{spec['id']}",
        None,
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
    assert run["budget"]["max_usd"] is None
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


DECLINED = (
    "git push of evo-agents to chat/{id} failed: git push failed (1): remote: no write access error: failed to push "
    "some refs to 'https://github.com/maycuatroi1/evo-agents.git' (origin declined it: pre-receive hook declined)"
)


def test_a_chat_run_whose_push_origin_declined_goes_on_in_its_session_and_another_failure_starts_a_new_one(
    client, hub, hub_db
):
    """Criterion a2 of chat-runs-start-fast: the commits of a run whose push the remote declined wait on chat/<first
    run> in its worktree, so the next message of the session goes on in it, on its worker, for that run to push them;
    a run that failed of any other cause leaves the next message a new session."""
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, worker, chat)
    first = spec["id"]
    error = DECLINED.format(id=first)
    moved(
        client,
        worker,
        first,
        "verifying",
        "failed",
        error=error,
        failure_cause="push_declined",
        usage={"total_cost_usd": 0.4},
    )
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(CROSS, ["reviewer"])]
    said = thread(client, hub["owner"], chat["id"], mention["id"])[-1]
    assert (said["author"]["kind"], said["body"]) == ("hub", f"Run #{first} of @reviewer failed: {error}")
    assert len(chat_runs_of(hub_db)) == 1, "a failed run wakes nothing by itself"

    # the next message goes on in the session, on its worker and branch, the session key unchanged
    again = sent(client, hub["owner"], chat["id"], "@reviewer push it again, please", mention["id"])
    ((_, _, _, _, owner_id), (second, state, resume_of, hop, by)) = chat_runs_of(hub_db)
    assert (again["mentions"][0]["run_id"], state, resume_of, hop, by) == (second, "queued", first, 1, owner_id)
    run = run_of(client, hub["owner"], second)
    assert (run["pinned_worker_id"], run["session_id"], run["branch"], run["chat_message_id"]) == (
        worker["id"],
        "sess-1",
        f"chat/{first}",
        again["id"],
    )
    assert {entry["branch"] for entry in run["repos"]} == {f"chat/{first}"}
    assert run["budget"]["max_usd"] is None
    goes_on = claim(client, worker)
    assert (goes_on["id"], goes_on["resume_of_run_id"], goes_on["session_id"]) == (second, first, "sess-1")
    assert goes_on["budget"]["spent_usd"] == pytest.approx(0.4)
    held = client.post(f"/v1/worker/runs/{second}/inbox", json={}, headers=worker["headers"]).json()["messages"]
    assert [item["text"].splitlines()[-1] for item in held] == ["@reviewer push it again, please"]

    # another person's mention in the thread still starts a session of their own
    theirs = chat_worker(client, hub["other"], "laptop")
    other = sent(client, hub["other"], chat["id"], "@reviewer and mine", mention["id"])
    mine = run_of(client, hub["other"], other["mentions"][0]["run_id"])
    assert (mine["dispatched_by"], mine["pinned_worker_id"], mine["resume_of_run_id"]) == (OTHER, theirs["id"], None)

    # a run of the session that fails of another cause leaves the next message a new session
    moved(client, worker, second, "running", session_id="sess-1")
    moved(client, worker, second, "failed", error="claude-code stopped at the run's time cap", failure_cause="time_cap")
    fresh = sent(client, hub["owner"], chat["id"], "@reviewer once more", mention["id"])
    run = run_of(client, hub["owner"], fresh["mentions"][0]["run_id"])
    assert (run["resume_of_run_id"], run["session_id"], run["pinned_worker_id"]) == (None, None, worker["id"])
    assert run["branch"] == f"chat/{run['id']}"


def test_a_chat_run_that_failed_without_a_declined_push_starts_a_new_session(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    for cause in (None, "credentials", "checkout"):
        mention, spec = started(client, hub, worker, chat)
        report_body = {"error": "git push of evo-agents to chat/1 failed: no luck"}
        moved(client, worker, spec["id"], "failed", **report_body, **({"failure_cause": cause} if cause else {}))
        later = sent(client, hub["owner"], chat["id"], "@reviewer again", mention["id"])
        run = run_of(client, hub["owner"], later["mentions"][0]["run_id"])
        assert (run["resume_of_run_id"], run["branch"]) == (None, f"chat/{run['id']}"), cause
        moved(client, worker, claim(client, worker)["id"], "running", "failed", error="stop here")


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


def test_a_chat_has_no_budget_and_its_runs_go_on_whatever_they_cost_under_a_time_cap(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio", slots=3)
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer", "fixer"])
    assert "budget_usd" not in chat
    mention, spec = started(client, hub, worker, chat)
    ended(client, worker, spec["id"], usage={"total_cost_usd": 50.0})
    again = sent(client, hub["owner"], chat["id"], "@fixer now you")
    assert again["mentions"][0]["refusal"] is None
    run = run_of(client, hub["owner"], again["mentions"][0]["run_id"])
    assert run["budget"] == {"max_usd": None, "max_turns": None, "max_seconds": chat_runs.TIME_CAP_SECONDS}
    assert client.patch(f"/v1/chats/{chat['id']}", json={"budget_usd": 20}, headers=hub["owner"]).status_code == 405


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


def calls(github, method: str, suffix: str) -> list:
    return [r for r in github.requests if r.method == method and r.path.endswith(suffix)]


def token_permissions(github, token: str) -> tuple[str, dict]:
    """(the App, the permissions) of an installation token the fake made."""
    made = github.app_tokens[token]
    return github.installations[made.installation].app, made.permissions


def pushed_branch(client, hub, github) -> tuple:
    """A chat run of @reviewer, running, whose branch is on GitHub: (worker, chat, mention, spec, body of its push)."""
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    branch = f"chat/{spec['id']}"
    github.push(MINE, "evo-agents", branch, "1" * 40)
    return worker, chat, mention, spec, {"repo": "evo-agents", "branch": branch, "head": "1" * 40}


def test_a_refused_pull_request_says_githubs_errors_and_the_next_push_opens_it(client, hub, hub_db, github):
    from evo_agents.hub.server import pulls

    worker, chat, mention, spec, body = pushed_branch(client, hub, github)
    refusal = {"resource": "PullRequest", "field": "base", "code": "invalid", "message": "base is not\n there"}
    github.pull_refusals.append((422, {"message": "Validation Failed", "errors": [refusal, "and one more"]}))
    refused = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert (refused["url"], refused["number"], refused["opened"]) == (None, None, False)
    assert len(calls(github, "POST", "/pulls")) == 1  # not the head: no second try
    said = message(client, hub["owner"], chat["id"], refused["message_id"])
    assert said["thread_root_id"] == mention["id"]
    assert said["body"] == (
        f"Pushed {body['branch']} of evo-agents. The hub did not open its draft pull request: GitHub answered 422 "
        f"while opening a pull request of {body['branch']} on {MINE}/evo-agents: Validation Failed (field base, code "
        "invalid: base is not there; and one more). The next push of this session tries again."
    )
    (opening,) = calls(github, "POST", "/pulls")
    app, permissions = token_permissions(github, opening.headers["authorization"].partition(" ")[2])
    assert app == "workers" and permissions == pulls.SERVER_PERMISSIONS["open"]
    assert permissions["contents"] == "read"  # GitHub checks the head and base of a private repo with it
    # the session's next push finds no pull request and opens it
    again = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "head": "2" * 40}).json()
    assert (again["number"], again["opened"]) == (1, True)
    made = github.pull_of(MINE, "evo-agents", body["branch"])
    assert made.draft and message(client, hub["owner"], chat["id"], again["message_id"])["body"].startswith(
        "Opened a draft pull request of evo-agents"
    )


def test_a_head_github_does_not_see_yet_is_tried_again(client, hub, hub_db, github, monkeypatch):
    from evo_agents.hub.server import pulls

    monkeypatch.setattr(pulls, "HEAD_WAITS", (0.0, 0.0))
    worker, chat, _, spec, body = pushed_branch(client, hub, github)
    missing = (422, {"message": "Validation Failed", "errors": [{"field": "head", "code": "invalid"}]})
    github.pull_refusals += [missing, missing]
    opened = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert (opened["number"], opened["opened"]) == (1, True)
    assert len(calls(github, "POST", "/pulls")) == 3
    # three times not there: the last refusal is said, with GitHub's errors
    github.close_by_hand(MINE, "evo-agents", body["branch"])
    github.pull_refusals += [missing, missing, missing]
    gone = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert gone["number"] is None and len(calls(github, "POST", "/pulls")) == 6
    said = message(client, hub["owner"], chat["id"], gone["message_id"])["body"]
    assert said.endswith("Validation Failed (field head, code invalid). The next push of this session tries again.")


def test_a_repo_without_drafts_gets_a_pull_request_titled_draft(client, hub, hub_db, github):
    worker, chat, _, spec, body = pushed_branch(client, hub, github)
    github.repos[(MINE.lower(), "evo-agents")].drafts = False
    opened = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert (opened["number"], opened["opened"]) == (1, True)
    made = github.pull_of(MINE, "evo-agents", body["branch"])
    assert not made.draft and made.title.startswith(f"{chat_runs.DRAFT_TITLE} @reviewer in Release talk")
    assert message(client, hub["owner"], chat["id"], opened["message_id"])["body"] == (
        f"Opened a pull request of evo-agents from {body['branch']}, its title starting with [Draft]: GitHub takes no "
        f"draft pull request on this repo. {opened['url']}"
    )
    again = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "head": "2" * 40}).json()
    assert (again["number"], again["opened"]) == (1, False)
    assert message(client, hub["owner"], chat["id"], again["message_id"])["body"] == (
        f"Pushed more commits of evo-agents to {body['branch']}, in its pull request: {opened['url']}"
    )


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


# The repos of a chat run on a daemon of LAZY_CHAT_REPOS (criterion a3 of chat-runs-start-fast)

HARNESS = "evo-agents-harness"  # the harness test_plans registers the project with, which it does not list as a repo
WITH_HARNESS = {**CHECKOUTS, f"{PROJECT}/{HARNESS}": {"path": f"/src/{HARNESS}", "branch": "main"}}
NO_NOTES = {name: held for name, held in WITH_HARNESS.items() if name != f"{PROJECT}/notes"}
OPEN = chat_runs.OPEN_COMMAND


def repos_of(run: dict) -> list[str] | None:
    return None if run["repos"] is None else [entry["repo"] for entry in run["repos"]]


def opened(client, worker: dict, run_id: int, repo: str):
    return in_chats(client, worker, run_id, "chat/repos", {"repo": repo})


def listed(prompt: str) -> list[str]:
    """The lines of the repos a chat run's prompt lists."""
    lines = prompt.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("Repositories of ")) + 1
    return lines[start : lines.index("", start)]


def test_a_lazy_daemons_chat_run_starts_with_the_harness_alone_and_another_with_every_checkout(client, hub, hub_db):
    studio = chat_worker(client, hub["owner"], "studio", checkouts=WITH_HARNESS, slots=3, features=runs.WORKER_FEATURES)
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=WITH_HARNESS)
    chat = created(client, hub["owner"], people=[OTHER])
    mine = run_of(
        client, hub["owner"], sent(client, hub["owner"], chat["id"], "@reviewer fix it")["mentions"][0]["run_id"]
    )
    assert mine["pinned_worker_id"] == studio["id"]
    assert mine["repos"] == [{"repo": HARNESS, "branch": f"chat/{mine['id']}"}]
    # a daemon that does not say LAZY_CHAT_REPOS gets every repo of the project it has a checkout of, as before
    said = sent(client, hub["other"], chat["id"], "@reviewer and mine")
    theirs = run_of(client, hub["other"], said["mentions"][0]["run_id"])
    assert theirs["pinned_worker_id"] == laptop["id"]
    assert repos_of(theirs) == [HARNESS, "evo-agents", "lumen-identity", "lumen-kb-docs", "notes"]
    # no checkout of the harness, or a project registered without one: the run starts with no repo
    studio["checkouts"] = CHECKOUTS
    beat(client, studio)
    bare = run_of(
        client, hub["owner"], sent(client, hub["owner"], chat["id"], "@reviewer again")["mentions"][0]["run_id"]
    )
    assert (bare["pinned_worker_id"], bare["repos"]) == (studio["id"], None)
    studio["checkouts"] = WITH_HARNESS
    beat(client, studio)
    unregistered = update(tables.projects).values(cluster=None, workspace=None, harness_path=None)
    sql(hub_db, unregistered.where(tables.projects.c.name == PROJECT))
    none = run_of(
        client, hub["owner"], sent(client, hub["owner"], chat["id"], "@reviewer last")["mentions"][0]["run_id"]
    )
    assert (none["pinned_worker_id"], none["repos"]) == (studio["id"], None)


def test_a_lazy_chat_runs_prompt_lists_every_repo_open_and_named_first_with_the_command_that_opens_it(
    client, hub, hub_db
):
    studio = chat_worker(client, hub["owner"], "studio", checkouts=NO_NOTES, features=runs.WORKER_FEATURES)
    chat = created(client, hub["owner"], people=[OTHER])
    sent(client, hub["other"], chat["id"], "lumen-identity looks fine to me.")  # before the thread: it names nothing
    root = sent(client, hub["owner"], chat["id"], "The LUMEN-KB-DOCS build is red since Tuesday.")
    sent(client, hub["owner"], chat["id"], "@reviewer look at evo-agents too, please", root["id"])
    spec = claim(client, studio)
    prompt = spec["prompt"]
    branch = f"chat/{spec['id']}"
    assert listed(prompt) == [
        f"- {HARNESS}: open, checked out on this worker: its worktree is {HARNESS}/, and `{OPEN} {HARNESS}` prints its "
        "path",
        f"- evo-agents: not open, checked out on this worker, named in the chat: `{OPEN} evo-agents` opens it",
        f"- lumen-kb-docs: not open, checked out on this worker, named in the chat: `{OPEN} lumen-kb-docs` opens it",
        f"- lumen-identity: not open, checked out on this worker: `{OPEN} lumen-identity` opens it",
        f"- notes: not open, no checkout on this worker: `{OPEN} notes` refuses it",
    ]
    assert "Repositories of the project, those open in this run first, then those the chat names:" in prompt
    assert (
        f"Open a repo with `{OPEN} REPO` before you read or change its code: the worker makes its worktree on the "
        f"branch {branch} under the current directory"
    ) in prompt
    assert f"The harness.yaml of {HARNESS} says what each repo of the project is for." in prompt
    assert len(prompt.encode()) <= runs.MAX_PROMPT_BYTES
    # the prompt of another daemon's chat run lists the run's repos as before, and no command that opens one
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=NO_NOTES)
    sent(client, hub["other"], chat["id"], "@reviewer and mine")
    other = claim(client, laptop)
    assert listed(other["prompt"]) == [
        f"- {name}: {name}/" for name in (HARNESS, "evo-agents", "lumen-identity", "lumen-kb-docs")
    ]
    assert OPEN not in other["prompt"]
    assert (
        f"You may change code in the repos below, each a worktree on the branch chat/{other['id']}" in other["prompt"]
    )


def test_a_lazy_chat_run_opens_a_repo_its_leases_pulls_and_next_turn_take(client, hub, hub_db, github):
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    studio = chat_worker(client, hub["owner"], "studio", checkouts=NO_NOTES, features=runs.WORKER_FEATURES)
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, studio, chat)
    first, branch = spec["id"], f"chat/{spec['id']}"
    assert spec["repos"] == [{"repo": HARNESS, "branch": branch}]
    leases = credentials_api.leased(client, studio, first)
    assert (credentials_api.by_name(leases), set(credentials_api.missing(leases))) == ({}, {HARNESS})
    pull = {"repo": "evo-agents", "branch": branch, "head": "1" * 40}
    refused = in_chats(client, studio, first, "chat/pulls", pull)
    assert refused.status_code == 422 and "does not work in evo-agents" in refused.json()["message"]

    answer = opened(client, studio, first, "evo-agents")
    assert answer.status_code == 200, answer.text
    both = [{"repo": HARNESS, "branch": branch}, {"repo": "evo-agents", "branch": branch}]
    assert answer.json() == {"repo": "evo-agents", "branch": branch, "added": True, "repos": both}
    again = opened(client, studio, first, "evo-agents")
    assert again.status_code == 200 and again.json() == {**answer.json(), "added": False}
    assert run_of(client, hub["owner"], first)["repos"] == both
    audited = select(tables.audit.c.action, tables.audit.c.target, tables.audit.c.project_id)
    rows = sql(hub_db, audited.where(tables.audit.c.action == "run.open_repo"))
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    assert rows == [("run.open_repo", f"{PROJECT}/chat run:{first} repo=evo-agents", project_id)]
    # the run's leases take the repo it opened, and so does its pull request
    leases = credentials_api.leased(client, studio, first)
    assert set(credentials_api.by_name(leases)) == {f"github-app:{MINE}"}
    assert set(credentials_api.missing(leases)) == {HARNESS}
    github.push(MINE, "evo-agents", branch, "1" * 40)
    made = in_chats(client, studio, first, "chat/pulls", pull)
    assert made.status_code == 201 and made.json()["number"] == 1, made.text

    # a repo the project does not list, or one the worker has no checkout of, is refused, and nothing changes
    unknown = opened(client, studio, first, "nope")
    assert unknown.status_code == 422
    assert unknown.json()["message"] == (
        f"project {PROJECT} has no repo nope: its repos are {HARNESS}, evo-agents, lumen-identity, lumen-kb-docs, notes"
    )
    studio["features"] = [name for name in runs.WORKER_FEATURES if name != runs.CLONES_CHECKOUTS]
    beat(client, studio, held=[first])  # a daemon that does not clone what it lacks
    unheld = opened(client, studio, first, "notes")
    assert unheld.status_code == 409
    assert unheld.json()["message"] == (
        f"worker studio has no checkout of notes of project {PROJECT}: the run cannot open it here"
    )
    studio["features"] = list(runs.WORKER_FEATURES)
    beat(client, studio, held=[first])  # one that does, for a repo no project lists an origin for
    without = update(tables.project_repos).values(origin=None).where(tables.project_repos.c.name == "notes")
    sql(hub_db, without)
    unknown_origin = opened(client, studio, first, "notes")
    assert unknown_origin.status_code == 409
    assert unknown_origin.json()["message"] == (
        f"worker studio has no checkout of notes of project {PROJECT}: and no project of the run's owner lists an "
        "origin to clone it from"
    )
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=NO_NOTES, features=runs.WORKER_FEATURES)
    assert opened(client, laptop, first, "lumen-identity").status_code == 404  # not its run
    assert run_of(client, hub["owner"], first)["repos"] == both

    # the next turn of the session starts with the repos the run opened, listed open first
    ended(client, studio, first)
    assert opened(client, studio, first, "lumen-identity").status_code == 404  # the run ended
    later = sent(client, hub["owner"], chat["id"], "@reviewer and the docs?", mention["id"])
    second = later["mentions"][0]["run_id"]
    assert run_of(client, hub["owner"], second)["repos"] == both
    goes_on = claim(client, studio)
    assert (goes_on["id"], goes_on["resume_of_run_id"], goes_on["repos"]) == (second, first, both)
    assert [line.split(":")[:2] for line in listed(goes_on["prompt"])[:2]] == [
        [f"- {HARNESS}", " open, checked out on this worker"],
        ["- evo-agents", " open, checked out on this worker"],
    ]
    moved(client, studio, second, "running")
    taken = client.post(f"/v1/worker/runs/{second}/inbox", json={}, headers=studio["headers"]).json()["messages"]
    client.post(f"/v1/worker/runs/{second}/inbox", json={"ack": taken[-1]["id"]}, headers=studio["headers"])
    ended(client, studio, second)

    # a run of another kind opens no repo here
    plan = {"body": credentials_api.plan_body()}
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{credentials_api.PLAN}", json=plan, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text
    body = {"plan_id": credentials_api.PLAN, "steps": [1]}
    assert client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"]).status_code == 201
    step = claim(client, studio)
    assert step["kind"] == "step"
    refused = opened(client, studio, step["id"], "evo-agents")
    assert refused.status_code == 404 and "only a chat run's agent opens a repo here" in refused.json()["message"]


# The turns of a session (criteria a5 and a6 of chat-runs-start-fast)

FIRST_ASK = "@reviewer please fix the flaky wait test"


def runs_listed(client, headers, **params) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def turn_of(run: dict) -> tuple:
    return (run["title"], run["session_run_id"], run["session_turn"], run["session_turns"], run["session_title"])


def taken_and_ended(client, worker: dict, run_id: int) -> None:
    """The agent of running chat run ``run_id`` takes the messages of its inbox, and its turn ends done."""
    taken = client.post(f"/v1/worker/runs/{run_id}/inbox", json={}, headers=worker["headers"]).json()["messages"]
    client.post(f"/v1/worker/runs/{run_id}/inbox", json={"ack": taken[-1]["id"]}, headers=worker["headers"])
    ended(client, worker, run_id)


def three_turns(client, hub, worker: dict, chat: dict) -> tuple:
    """A session of @reviewer in ``chat`` that ``worker`` runs: its first run woken by the mention, its second by a
    message left in the first one's inbox, its third by a message in the thread once the second ended; the third
    claimed and running. (mention, the ids of the three runs)."""
    mention, spec = started(client, hub, worker, chat, FIRST_ASK)
    first = spec["id"]
    sent(client, hub["owner"], chat["id"], "@reviewer one more thing: the retries", mention["id"])  # in its inbox
    ended(client, worker, first)
    second = claim(client, worker)["id"]
    moved(client, worker, second, "running")
    taken_and_ended(client, worker, second)
    later = sent(client, hub["owner"], chat["id"], "@reviewer and the docs?", mention["id"])
    third = later["mentions"][0]["run_id"]
    assert claim(client, worker)["id"] == third
    moved(client, worker, third, "running")
    return mention, (first, second, third)


def test_each_turn_of_a_chat_session_is_titled_by_its_message_and_the_pull_request_by_the_session(
    client, hub, hub_db, github
):
    """Criterion a5: a run that goes on in a session is titled by the message that woke it, the person's or the first
    one the run before left in its inbox, not by the session's first run; the API says of each chat run the first run
    of its session and its turn; the pull request of the session's branch keeps the first run's title."""
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    _, (first, second, third) = three_turns(client, hub, worker, chat)
    session = f"@reviewer in Release talk: {FIRST_ASK}"
    assert [turn_of(run_of(client, hub["owner"], run_id)) for run_id in (first, second, third)] == [
        (session, first, 1, 3, session),
        ("@reviewer in Release talk: @reviewer one more thing: the retries", first, 2, 3, session),
        ("@reviewer in Release talk: @reviewer and the docs?", first, 3, 3, session),
    ]
    # the third turn pushes the session's branch: its pull request has the session's title, not the turn's
    branch = f"chat/{first}"
    github.push(MINE, "evo-agents", branch, "1" * 40)
    body = {"repo": "evo-agents", "branch": branch, "head": "1" * 40}
    assert in_chats(client, worker, third, "chat/pulls", body).status_code == 201
    assert github.pull_of(MINE, "evo-agents", branch).title == session
    # a run of another kind, and a chat session of one run, say so
    chat_worker(client, hub["other"], "laptop")
    other = sent(client, hub["other"], chat["id"], "@reviewer what about mine?")
    alone = run_of(client, hub["other"], other["mentions"][0]["run_id"])
    assert turn_of(alone)[1:] == (alone["id"], 1, 1, alone["title"])


def test_the_runs_list_groups_a_chat_sessions_turns_as_its_latest_and_counts_and_pages_them(client, hub, hub_db):
    """Criterion a6 on the API's side: with group=session the list holds a chat session once, as its latest turn, its
    state that turn's for the state filter, the counts and the total; a search that finds any turn lists the session;
    session_run_id lists the turns of one session. Without group, every run is listed as before."""
    worker = chat_worker(client, hub["owner"], "studio", slots=2)
    chat = created(client, hub["owner"], people=[OTHER])
    _, (first, second, third) = three_turns(client, hub, worker, chat)
    taken_and_ended(client, worker, third)
    fourth = sent(client, hub["owner"], chat["id"], "@reviewer last one, the changelog")["mentions"][0]["run_id"]
    assert run_of(client, hub["owner"], fourth)["session_turn"] == 1  # a new thread: a new session
    plan = {"body": credentials_api.plan_body()}
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{credentials_api.PLAN}", json=plan, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text
    body = {"plan_id": credentials_api.PLAN, "steps": [1]}
    (step,) = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"]).json()
    assert turn_of(run_of(client, hub["owner"], step["id"]))[1:] == (None, None, None, None)

    every = runs_listed(client, hub["owner"])
    assert [run["id"] for run in every["runs"]] == [step["id"], fourth, third, second, first]
    assert (every["total"], every["counts"]["done"], every["counts"]["queued"]) == (5, 3, 2)

    grouped = runs_listed(client, hub["owner"], group="session")
    assert [run["id"] for run in grouped["runs"]] == [step["id"], fourth, third]
    assert (grouped["total"], grouped["counts"]["done"], grouped["counts"]["queued"]) == (3, 1, 2)
    assert [run["session_turns"] for run in grouped["runs"]] == [None, 1, 3]
    done = runs_listed(client, hub["owner"], group="session", state="done")
    assert ([run["id"] for run in done["runs"]], done["total"]) == ([third], 1)
    paged = runs_listed(client, hub["owner"], group="session", limit=2, offset=2)
    assert ([run["id"] for run in paged["runs"]], paged["total"]) == ([third], 3)

    # the search finds the session by an earlier turn, and lists it as its latest
    found = runs_listed(client, hub["owner"], group="session", q="one more thing")
    assert ([run["id"] for run in found["runs"]], found["total"], found["counts"]["done"]) == ([third], 1, 1)
    by_number = runs_listed(client, hub["owner"], group="session", q=f"#{first}")
    assert [run["id"] for run in by_number["runs"]] == [third]
    assert runs_listed(client, hub["owner"], q="one more thing")["total"] == 1  # ungrouped: the turn alone

    turns = runs_listed(client, hub["owner"], session_run_id=first)
    assert ([run["id"] for run in turns["runs"]], turns["total"]) == ([third, second, first], 3)
    assert runs_listed(client, hub["owner"], session_run_id=step["id"])["total"] == 0
    refused = client.get(f"/v1/projects/{PROJECT}/runs", params={"group": "plan"}, headers=hub["owner"])
    assert refused.status_code == 422


# How far a run is, under the message it took (criterion a7 of the plan chat-runs-start-fast)


def noted(client, worker: dict, run_id: int, *bodies: dict, first: int = 1) -> None:
    """The worker's notes (system events) of run ``run_id``, numbered from ``first``, as its spool sends them."""
    at = datetime.now(UTC).isoformat()
    events = [{"seq": first + n, "at": at, "kind": "system", "body": body} for n, body in enumerate(bodies)]
    response = client.post(f"/v1/worker/runs/{run_id}/events", json={"events": events}, headers=worker["headers"])
    assert response.status_code == 200, response.text


def progress_of(client, headers, chat_id: int, message_id: int) -> list[tuple]:
    """The progress under message ``message_id`` as ``headers``' member reads the chat: (run, step, worker, repo)."""
    shown = message(client, headers, chat_id, message_id)["progress"]
    assert all(item["message_id"] == message_id and item["handle"] == "reviewer" for item in shown), shown
    return [(item["run_id"], item["step"], item["worker"], item["repo"]) for item in shown]


def written(db, chat_id: int) -> tuple:
    """What a chat holds that a step of a run must not add: its messages, its events, and every notice."""
    cm, ce, n = tables.chat_messages, tables.chat_events, tables.notifications
    count = select(func.count())
    return (
        sql(db, count.select_from(cm).where(cm.c.chat_id == chat_id)),
        sql(db, count.select_from(ce).where(ce.c.chat_id == chat_id)),
        sql(db, count.select_from(n)),
    )


SETUP = [  # what a daemon notes as it gets a chat run ready, and the step the thread shows after each
    ({"text": "Run claimed by worker studio.", "setup": "claimed"}, "claimed", None),
    ({"text": "Checking access of the run: 1 repo(s): evo-agents.", "setup": "checking"}, "checking", None),
    (
        {"text": "Fetching origin in /src/evo-agents.", "repo": "evo-agents", "setup": "opening"},
        "opening",
        "evo-agents",
    ),
    ({"text": "Worktree /w/evo-agents of evo-agents.", "repo": "evo-agents"}, "opening", "evo-agents"),
    ({"text": "Fetching origin in /src/notes.", "repo": "notes", "setup": "opening"}, "opening", "notes"),
    ({"text": "The run's worktrees are ready: starting claude-code.", "setup": "starting"}, "starting", None),
]


def test_a_chat_runs_progress_shows_under_its_message_by_rule_until_its_agent_answers(client, hub, hub_db):
    """The answer to the mention already says the agent took it and the worker its run waits for. Each step of the
    run's setup its worker notes then replaces the one before under the message (a note that names none keeps it),
    working once the agent runs, opening again for a repo the agent asks for; the agent's reply ends it. No step writes
    a chat message, a chat event or a notice, and the reactions follow the run as before."""
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer please fix the flaky wait test")
    run_id = mention["mentions"][0]["run_id"]
    (ack,) = mention["progress"]
    assert (ack["message_id"], ack["run_id"], ack["handle"], ack["step"], ack["worker"], ack["repo"]) == (
        mention["id"],
        run_id,
        "reviewer",
        "waiting",
        "studio",
        None,
    )
    assert progress_of(client, hub["other"], chat["id"], mention["id"]) == [(run_id, "waiting", "studio", None)]
    assert claim(client, worker)["id"] == run_id
    assert progress_of(client, hub["other"], chat["id"], mention["id"]) == [(run_id, "claimed", "studio", None)]
    before = written(hub_db, chat["id"])
    for seq, (body, step, repo) in enumerate(SETUP, start=1):
        noted(client, worker, run_id, body, first=seq)
        assert progress_of(client, hub["other"], chat["id"], mention["id"]) == [(run_id, step, "studio", repo)], body
    moved(client, worker, run_id, "running", session_id="sess-1")
    assert progress_of(client, hub["other"], chat["id"], mention["id"]) == [(run_id, "working", "studio", None)]
    seq = len(SETUP) + 1
    noted(
        client,
        worker,
        run_id,
        {"text": "The agent asks to open lumen-identity.", "repo": "lumen-identity", "setup": "opening"},
        first=seq,
    )
    assert progress_of(client, hub["other"], chat["id"], mention["id"]) == [
        (run_id, "opening", "studio", "lumen-identity")
    ]
    noted(
        client,
        worker,
        run_id,
        {"text": "The agent opened lumen-identity.", "repo": "lumen-identity", "setup": "working"},
        first=seq + 1,
    )
    assert progress_of(client, hub["other"], chat["id"], mention["id"]) == [(run_id, "working", "studio", None)]
    assert written(hub_db, chat["id"]) == before, "the steps write no message, chat event or notice"
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(EYES, ["reviewer"])]

    reply(client, worker, run_id, "Replaced the sleep with a poll.")
    assert progress_of(client, hub["other"], chat["id"], mention["id"]) == []
    ended(client, worker, run_id)
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(CHECK, ["reviewer"])]
    assert all(not item["progress"] for item in thread(client, hub["owner"], chat["id"], mention["id"]))


def test_a_message_to_a_working_run_waits_in_its_inbox_until_the_agent_takes_it_and_answers(client, hub, hub_db):
    """A message that reaches the inbox of the turn that runs says so at once, and keeps saying it while the agent
    answers what it had before; once the worker hands it to the agent the run works on it, until the next answer."""
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, worker, chat)
    later = sent(client, hub["owner"], chat["id"], "@reviewer also look at retries", mention["id"])
    assert [(item["run_id"], item["step"], item["worker"]) for item in later["progress"]] == [
        (spec["id"], "inbox", "studio")
    ]
    assert progress_of(client, hub["owner"], chat["id"], mention["id"]) == [(spec["id"], "working", "studio", None)]
    reply(client, worker, spec["id"], "The wait test polls now.")  # the answer of the turn that had not taken it
    assert progress_of(client, hub["owner"], chat["id"], mention["id"]) == []
    assert progress_of(client, hub["owner"], chat["id"], later["id"]) == [(spec["id"], "inbox", "studio", None)]
    inbox = client.post(f"/v1/worker/runs/{spec['id']}/inbox", json={}, headers=worker["headers"]).json()["messages"]
    client.post(f"/v1/worker/runs/{spec['id']}/inbox", json={"ack": inbox[0]["id"]}, headers=worker["headers"])
    assert progress_of(client, hub["owner"], chat["id"], later["id"]) == [(spec["id"], "working", "studio", None)]
    reply(client, worker, spec["id"], "Retries are bounded too.")
    assert progress_of(client, hub["owner"], chat["id"], later["id"]) == []


def test_a_message_its_run_left_untaken_shows_the_run_that_goes_on_with_it(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    reply(client, worker, spec["id"], "Done.")
    later = sent(client, hub["owner"], chat["id"], "one more thing", mention["id"])  # one person: no @ in the thread
    ended(client, worker, spec["id"])
    (_, (next_id, state, resume_of, _, _)) = chat_runs_of(hub_db)
    assert (state, resume_of) == ("queued", spec["id"])
    assert progress_of(client, hub["owner"], chat["id"], later["id"]) == [(next_id, "waiting", "studio", None)]
    assert progress_of(client, hub["owner"], chat["id"], mention["id"]) == [], "its session answered it"
    assert claim(client, worker)["id"] == next_id
    assert progress_of(client, hub["owner"], chat["id"], later["id"]) == [(next_id, "claimed", "studio", None)]


def test_a_failed_chat_runs_progress_ends_and_the_thread_says_why(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    assert progress_of(client, hub["owner"], chat["id"], mention["id"]) == [(spec["id"], "working", "studio", None)]
    moved(client, worker, spec["id"], "failed", error="git fetch in /src/evo-agents failed", failure_cause="checkout")
    assert progress_of(client, hub["owner"], chat["id"], mention["id"]) == []
    answer = thread(client, hub["owner"], chat["id"], mention["id"])[-1]
    assert answer["body"] == f"Run #{spec['id']} of @reviewer failed: git fetch in /src/evo-agents failed"
    assert reactions(client, hub["owner"], chat["id"], mention["id"]) == [(CROSS, ["reviewer"])]


def test_who_may_not_read_a_chat_run_sees_nothing_more_of_it_in_the_thread(client, hub, hub_db):
    """Progress names a worker and repos: it shows to a member who may read the run (a grant on its project, its
    default label through the project's hub sink), and to nobody else, who reads the chat as before."""
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention = sent(client, hub["owner"], chat["id"], "@reviewer please fix the flaky wait test")
    run_id = mention["mentions"][0]["run_id"]
    assert claim(client, worker)["id"] == run_id
    assert progress_of(client, hub["other"], chat["id"], mention["id"]) == [(run_id, "claimed", "studio", None)]
    # the hub's sink no longer carries the project's default label: the runs of no plan read as none on the hub
    closed = [{**sink, "clearance": {"level": "public", "location": "any"}} for sink in OPEN_SINKS]
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": closed}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"]).status_code == 200
    assert client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=hub["other"]).status_code == 404
    seen = message(client, hub["other"], chat["id"], mention["id"])
    assert seen["progress"] == [] and seen["mentions"][0]["run_id"] == run_id
    events = client.get(f"/v1/chats/{chat['id']}/events", params={"progress": True}, headers=hub["other"]).json()
    assert events["progress"] == []
    assert next(e for e in events["events"] if e["kind"] == "message")["message"]["progress"] == []
