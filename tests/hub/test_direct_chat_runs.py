"""What a message of a direct chat hands its agent (``evo_agents.hub.server.chats``, ``chat_direct``, ``chat_runs``), as
criterion a2 of the plan agent-direct-chats names it: each message of the person outside a thread is a task, handed
to the chat's agent without an @, as a mention would (a new session, dispatched by the sender, hop 1, within the
chat's budget and MAX_RUNS_PER_MESSAGE); an @ of the agent makes one run, not two; a reply in the thread of the agent's
answer goes on in that session; with no worker the hub says why in the thread. The prompt of a run in a direct chat
shows the latest turns (the person's messages and the agent's answers in the threads before) within SNAPSHOT_BYTES,
the oldest left out first. A group chat is as it was: a message without an @ makes no run.

Opening a direct chat and its members are in ``tests.hub.test_direct_chats``."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import chat_runs, tables
from evo_agents.hub.server.chats import NOT_IN_DIRECT
from tests.hub import test_chat_runs as group_runs
from tests.hub.live import sql
from tests.hub.test_chat_runs import (
    OTHER,
    OWNER,
    PROJECT,
    chat_runs_of,
    chat_worker,
    claim,
    created,
    ended,
    moved,
    reply,
    run_of,
    sent,
    thread,
)

app_key, client, hub = group_runs.app_key, group_runs.client, group_runs.hub  # fixtures: owner admin, @reviewer, @fixer


def direct(client, headers, handle: str = "reviewer") -> dict:
    response = client.put(f"/v1/chats/direct/{handle}", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def handed(message: dict) -> list[tuple]:
    return [(m["name"], m["via"], m["run_id"] is not None, m["refusal"]) for m in message["mentions"]]


def claimed(client, hub, worker: dict, message: dict) -> dict:
    """The run ``worker`` claims, which ``message`` started."""
    spec = claim(client, worker)
    assert spec is not None and run_of(client, hub["owner"], spec["id"])["chat_message_id"] == message["id"], spec
    return spec


def turn(client, hub, worker: dict, chat: dict, ask: str, answer: str) -> tuple[dict, dict]:
    """The owner's message ``ask`` in ``chat``, its run claimed, the agent's ``answer`` in its thread, the run done:
    (the message, the run's spec)."""
    message = sent(client, hub["owner"], chat["id"], ask)
    spec = claimed(client, hub, worker, message)
    moved(client, worker, spec["id"], "running", session_id=f"sess-{spec['id']}")
    reply(client, worker, spec["id"], answer)
    ended(client, worker, spec["id"])
    return message, spec


# A message is a task


def test_a_direct_message_without_an_at_hands_the_agent_work_in_a_new_session(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio", slots=2)
    chat = direct(client, hub["owner"])
    first = sent(client, hub["owner"], chat["id"], "The nightly build is red since Tuesday, please look.")
    assert handed(first) == [("reviewer", "direct", True, None)]
    ((made, state, resume_of, hop, by),) = chat_runs_of(hub_db)
    assert (first["mentions"][0]["run_id"], state, resume_of, hop, by) == (
        made,
        "queued",
        None,
        1,
        user_id(hub_db, OWNER),
    )
    run = run_of(client, hub["owner"], made)
    assert (run["kind"], run["profile"], run["dispatched_by"], run["pinned_worker_id"]) == (
        "chat",
        "reviewer",
        OWNER,
        worker["id"],
    )
    assert (run["chat_id"], run["chat_message_id"], run["branch"]) == (chat["id"], first["id"], f"chat/{made}")
    second = sent(client, hub["owner"], chat["id"], "And the release notes of 0.22 too.")
    assert handed(second) == [("reviewer", "direct", True, None)]
    (_, (other, _, other_resume, other_hop, _)) = chat_runs_of(hub_db)
    assert (second["mentions"][0]["run_id"], other_resume, other_hop) == (other, None, 1)  # its own session
    assert run_of(client, hub["owner"], other)["branch"] == f"chat/{other}"


def test_an_at_of_the_direct_agent_makes_one_run_and_an_outsider_is_told_to_use_a_group(client, hub, hub_db):
    chat_worker(client, hub["owner"], "studio", slots=3)
    chat = direct(client, hub["owner"])
    named = sent(client, hub["owner"], chat["id"], "@reviewer fix the flaky wait test")
    assert handed(named) == [("reviewer", "handle", True, None)]
    assert len(chat_runs_of(hub_db)) == 1
    outsider = sent(client, hub["owner"], chat["id"], "@fixer can you take this one?")
    refusal = NOT_IN_DIRECT.format(name="fixer")
    assert handed(outsider) == [("fixer", None, False, refusal), ("reviewer", "direct", True, None)]
    assert len(chat_runs_of(hub_db)) == 2
    answer = thread(client, hub["owner"], chat["id"], outsider["id"])[-1]
    assert (answer["author"]["kind"], answer["body"]) == ("hub", refusal)


def test_a_reply_in_the_thread_of_a_direct_chat_goes_on_in_its_run_session(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = direct(client, hub["owner"])
    asked, spec = turn(
        client, hub, worker, chat, "Why is the wait test flaky?", "It sleeps 5 seconds; a poll would do."
    )
    answer = thread(client, hub["owner"], chat["id"], asked["id"])[-1]
    assert (answer["author"]["handle"], answer["run_id"]) == ("reviewer", spec["id"])
    later = sent(client, hub["owner"], chat["id"], "then replace it with a poll", answer["id"])
    assert handed(later) == [("reviewer", "thread", True, None)]
    (_, (next_id, state, resume_of, hop, _)) = chat_runs_of(hub_db)
    assert (later["mentions"][0]["run_id"], state, resume_of, hop) == (next_id, "queued", spec["id"], 1)
    assert run_of(client, hub["owner"], next_id)["branch"] == f"chat/{spec['id']}"


def test_an_answer_naming_its_own_agent_is_refused_nothing_and_hands_no_work(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = direct(client, hub["owner"])
    asked, _ = turn(client, hub, worker, chat, "Who are you?", "I am @reviewer; @fixer knows the CI better.")
    answer = thread(client, hub["owner"], chat["id"], asked["id"])[-1]
    assert answer["author"]["handle"] == "reviewer"
    assert [(m["name"], m["run_id"], m["refusal"]) for m in answer["mentions"]] == [
        ("reviewer", None, None),
        ("fixer", None, NOT_IN_DIRECT.format(name="fixer")),
    ]
    assert len(chat_runs_of(hub_db)) == 1


def test_a_direct_message_with_no_worker_gets_the_hubs_reason_and_makes_no_run(client, hub, hub_db):
    chat = direct(client, hub["owner"])
    asked = sent(client, hub["owner"], chat["id"], "look at the diff of run 120")
    ((name, via, made, refusal),) = handed(asked)
    assert (name, via, made) == ("reviewer", "direct", False)
    assert refusal.startswith(f"@reviewer: {OWNER} has no worker that takes runs of project {PROJECT}")
    answer = thread(client, hub["owner"], chat["id"], asked["id"])[-1]
    assert (answer["author"]["kind"], answer["body"]) == ("hub", refusal)
    assert chat_runs_of(hub_db) == []


def test_a_direct_message_once_the_budget_is_spent_makes_no_run_and_says_so(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = direct(client, hub["owner"])
    lowered = client.patch(f"/v1/chats/{chat['id']}", json={"budget_usd": 0.5}, headers=hub["owner"])
    assert lowered.status_code == 200, lowered.text  # the person of a direct chat is its creator
    message = sent(client, hub["owner"], chat["id"], "first task")
    spec = claim(client, worker)
    moved(client, worker, spec["id"], "running", session_id="sess-1")
    ended(client, worker, spec["id"], usage={"total_cost_usd": 0.6})
    assert run_of(client, hub["owner"], spec["id"])["chat_message_id"] == message["id"]
    spent = sent(client, hub["owner"], chat["id"], "second task")
    assert handed(spent) == [
        (
            "reviewer",
            "direct",
            False,
            "@reviewer: the chat's budget of $0.50 for 24 hours is spent: its agents' runs cost $0.60 in that time; "
            "the chat's creator may raise it",
        )
    ]
    assert len(chat_runs_of(hub_db)) == 1


def test_a_direct_agent_deleted_since_takes_no_new_run_and_the_hub_says_so(client, hub, hub_db):
    chat_worker(client, hub["owner"], "studio")
    chat = direct(client, hub["owner"])
    assert client.delete(f"/v1/projects/{PROJECT}/agents/reviewer", headers=hub["owner"]).status_code == 200
    asked = sent(client, hub["owner"], chat["id"], "are you there?")
    assert handed(asked) == [("reviewer", "direct", False, "@reviewer: @reviewer was deleted: it takes no new work")]
    assert thread(client, hub["owner"], chat["id"], asked["id"])[-1]["author"]["kind"] == "hub"
    assert chat_runs_of(hub_db) == []


def test_a_group_chat_of_one_person_and_one_agent_makes_no_run_without_an_at(client, hub, hub_db):
    chat_worker(client, hub["owner"], "studio")
    group = created(client, hub["owner"], title="Just me")
    quiet = sent(client, hub["owner"], group["id"], "a note to myself")
    assert quiet["mentions"] == [] and chat_runs_of(hub_db) == []
    pair = created(client, hub["owner"], title="The two of us", people=[OTHER])
    assert sent(client, hub["owner"], pair["id"], "hello")["mentions"] == [] and chat_runs_of(hub_db) == []


# The prompt


def test_a_direct_chat_run_prompt_shows_the_latest_turns_within_the_snapshot_bytes(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = direct(client, hub["owner"])
    filler = "x" * 2000
    for n in range(1, 5):
        turn(client, hub, worker, chat, f"Task {n}: {filler}", f"Answer {n}: {filler}")
    last = sent(client, hub["owner"], chat["id"], "Task 5: sum up what you did.")
    prompt = claimed(client, hub, worker, last)["prompt"]
    assert prompt.startswith(f"You are @reviewer (Reviewer), an agent of project {PROJECT} on the evo-agents hub")
    assert f"in a direct chat with {OWNER} (#{chat['id']})" in prompt
    assert f"This is a direct chat of {OWNER} and you" in prompt
    shortened = prompt[prompt.index("# The chat, shortened") : prompt.index("# The message to answer")]
    assert "## Earlier turns of this direct chat" in shortened and "## The chat before the thread" not in shortened
    assert "Task 4: " in shortened and "Answer 4: " in shortened  # the person's message and the agent's answer
    assert shortened.index("Task 4: ") < shortened.index("Answer 4: ")
    assert shortened.index("Answer 3: ") < shortened.index("Task 4: ")
    assert "Task 1: " not in shortened, "the oldest turns are left out first"
    assert "older message(s) of the chat are left out of this prompt" in shortened
    blocks = shortened.split("\n", 1)[1].strip()
    marks = len(chat_runs.CUT_MARK.encode()) + 64  # the cut mark and the heading, besides the messages
    assert len(blocks.encode()) <= chat_runs.SNAPSHOT_BYTES + marks
    assert prompt.rstrip().endswith("Task 5: sum up what you did.")


def test_a_group_chat_run_prompt_keeps_the_chat_before_the_thread(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    sent(client, hub["other"], chat["id"], "The nightly build is red since Tuesday.")
    sent(client, hub["owner"], chat["id"], "@reviewer why?")
    spec = claim(client, worker)
    assert "## The chat before the thread" in spec["prompt"] and "Earlier turns" not in spec["prompt"]
    assert f"in the group chat 'Release talk' (#{chat['id']})" in spec["prompt"]
    assert "This is a direct chat" not in spec["prompt"]


def test_the_snapshot_of_direct_turns_holds_the_latest_thirty_messages():
    before = [{"id": n, "author": OWNER, "at": None, "body": f"turn {n}"} for n in range(1, 41)]
    blocks = chat_runs.snapshot(None, [], before, direct=True)
    assert blocks[0] == chat_runs.CUT_MARK.format(left=10)
    assert blocks[1] == "## Earlier turns of this direct chat"
    assert [block.split("\n")[1] for block in blocks[2:]] == [f"turn {n}" for n in range(11, 41)]
    group = chat_runs.snapshot(None, [], before)
    assert group[1] == "## The chat before the thread" and len(group) == 2 + chat_runs.BEFORE_MESSAGES
