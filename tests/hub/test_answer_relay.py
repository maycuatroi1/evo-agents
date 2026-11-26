"""The answer of an asked agent reaches the agent that asked it whole (problem P-24 of evo-agents).

Three shapes of loss seen in the Work chats of 2026-11-15..17:

- the asking agent got the hub's line of the asked run's push instead of its answer, when the branch had no commit
  its base lacked (``chat_runs.nothing_text``), which ``is_pull_text`` did not know (runs #8212, #9025);
- the answer came cut: the message that wakes the asking run was clipped to 4 KiB, and a message of a thread to an
  agent's inbox to 8 KiB, while an answer is a chat message of up to 16 KiB (run #9177, about 361 bytes left out);
- a forward hit a deploy of the hub (``tests/worker/test_agent_command_retry.py``).
"""

from __future__ import annotations

import pytest

from evo_agents.hub import chat_runs, runs
from tests.hub import pg


@pytest.mark.parametrize(
    "why",
    [
        "chat/41 has no commit that main lacks",
        "chat/41 changes no file against main",
        "chat/41 changes only .venv, which a run makes beside the work and never commits",
    ],
)
@pytest.mark.parametrize("closed", [None, "https://github.com/lumen-org/evo-agents/pull/3"])
def test_the_line_of_a_push_with_nothing_to_merge_is_not_an_answer(why, closed):
    said = chat_runs.nothing_text("evo-agents-harness", "chat/41", why, closed)
    assert chat_runs.is_pull_text(said), said
    assert not chat_runs.is_pull_text(said + "\n\nThe answer: the export service signs them.")
    assert not chat_runs.is_pull_text("Pushed chat/41 of evo-agents, but the tests still fail: see the log.\nMore.")


def test_a_message_of_the_thread_reaches_the_inbox_whole():
    body = "also the retries\n" + "y" * 15_000  # a chat message is at most 16 KiB
    text = chat_runs.inbox_text("owner", 9, body, answers=(8, "reviewer", "z" * 5_000))
    assert body in text and "[cut by the hub" in text, "only the quote of the message it answers is cut"
    assert len(text.encode()) <= runs.MAX_INBOX_BYTES


def test_the_thread_line_names_the_message_that_holds_the_whole_answer():
    pytest.importorskip("fastapi", reason="the hub's server needs the hub-server extra, evo-ak[hub-server]")
    from evo_agents.hub.server.agents.service import asks as agent_asks

    mark = agent_asks.THREAD_CUT_MARK.replace("{message}", "19900")
    line = runs.clip("@ea-dev answered (run #9177):\n\n" + "x" * 4_300, agent_asks.ANSWER_IN_THREAD, mark)
    assert len(line.encode()) <= agent_asks.ANSWER_IN_THREAD
    assert line.endswith("bytes left out; message #19900 has the whole answer]")
    assert "the plan on the hub" not in line


if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.server.agents.service import asks as agent_asks
    from tests.hub import test_credentials_api as credentials_api
    from tests.hub.live import sql
    from tests.hub.test_chat_runs import (  # noqa: F401
        MINE,
        WORKERS_APP,
        app_key,
        chat_worker,
        claim,
        client,
        created,
        ended,
        hub,
        in_chats,
        moved,
        recover,
        reply,
        sent,
        started,
    )
    from tests.hub.test_run_access import inbox_texts
    from tests.hub.test_system_handoff import asked, system_desk

    REPO = "evo-agents"

    def answer_of(size: int) -> str:
        """An answer of about ``size`` bytes whose last line says it is the end."""
        lines = [
            f"- finding {n}: the wait helper backs off from 1 s to 60 s, measured on build {n}" for n in range(999)
        ]
        text = "Fixed in `wait.py`:\n\n" + "\n".join(lines)
        return text.encode()[:size].decode("utf-8", "ignore").rstrip() + "\n\nEND OF THE ANSWER"

    def asked_and_answered(client, hub, hub_db, github, answer: str) -> dict:
        """@reviewer asks @fixer, @fixer answers ``answer`` then pushes a branch with no commit main lacks, and its run
        ends: the ids of the wait, the run asked and the message of its answer."""
        credentials_api.install(github, REPO, permissions=WORKERS_APP)
        github.add_repo(MINE, REPO)
        studio, fixer, _, _, parent = system_desk(client, hub)
        made = asked(client, studio, parent, "fixer", "fix the retry loop of the wait helper")
        child = made["run_id"]
        moved(client, studio, parent, "waiting")
        recover(client)
        assert claim(client, fixer)["id"] == child
        moved(client, fixer, child, "running", session_id="sess-fix")
        said = reply(client, fixer, child, answer)
        branch = f"chat/{child}"
        github.hold(MINE, REPO, "main", "1" * 40)  # the agent's commits went into main another way
        github.push(MINE, REPO, branch, "1" * 40)
        push = in_chats(client, fixer, child, "chat/pulls", {"repo": REPO, "branch": branch, "head": "1" * 40})
        assert push.status_code == 201, push.text
        m = tables.chat_messages
        (line,) = sql(hub_db, select(m.c.body).where(m.c.id == push.json()["message_id"]))[0]
        assert line.startswith(f"Pushed {branch} of {REPO}, but {branch} has no commit that main lacks")
        ended(client, fixer, child)
        return {"wait": made["decision_id"], "child": child, "message": said.get("message_id"), "studio": studio}

    def woke_with(hub_db, wait: int) -> str:
        i = tables.run_inbox
        (body,) = sql(hub_db, select(i.c.body).where(i.c.decision_id == wait))[0]
        return body

    def thread_lines(hub_db, child: int) -> list[str]:
        m = tables.chat_messages
        query = select(m.c.body).where(m.c.body.startswith(f"@fixer answered (run #{child}):"))
        return [body for (body,) in sql(hub_db, query)]

    def test_the_asker_gets_the_answer_not_the_line_of_a_push_with_nothing_to_merge(client, hub, hub_db, github):
        answer = "Fixed in `wait.py`: the loop backs off, and the test passes ten times."
        done = asked_and_answered(client, hub, hub_db, github, answer)
        woke = woke_with(hub_db, done["wait"])
        assert answer in woke and "has no commit" not in woke
        (line,) = thread_lines(hub_db, done["child"])
        assert answer in line and "has no commit" not in line

    def test_the_asker_gets_a_long_answer_whole_and_the_thread_its_start(client, hub, hub_db, github):
        answer = answer_of(9_000)  # over the 4 KiB the wake message had, and the 8 KiB of an inbox message
        done = asked_and_answered(client, hub, hub_db, github, answer)
        woke = woke_with(hub_db, done["wait"])
        assert answer in woke and f"Branch: chat/{done['child']} of {REPO}" in woke and "[cut by the hub" not in woke
        d = tables.decisions
        (kept,) = sql(hub_db, select(d.c.answer_text).where(d.c.id == done["wait"]))[0]
        assert len(kept.encode()) <= runs.MAX_ANSWER_BYTES and kept.startswith(f"Decision #{done['wait']} (wait) woke")
        (line,) = thread_lines(hub_db, done["child"])
        assert len(line.encode()) <= agent_asks.ANSWER_IN_THREAD
        m = tables.chat_messages
        (message_id,) = sql(hub_db, select(m.c.id).where(m.c.body == answer))[0]
        assert line.endswith(f"bytes left out; message #{message_id} has the whole answer]")

    def test_a_long_message_of_the_thread_reaches_the_running_agent_whole(client, hub, hub_db):
        worker = chat_worker(client, hub["owner"], "studio")
        chat = created(client, hub["owner"])
        mention, spec = started(client, hub, worker, chat)
        body = "and the docs too:\n" + "the export links expire after an hour. " * 300  # about 12 KB
        sent(client, hub["owner"], chat["id"], body, reply_to=mention["id"])
        texts = inbox_texts(client, worker, spec["id"])
        assert any(body.strip() in text for text in texts), [len(text) for text in texts]

    def test_a_later_run_of_the_same_agent_in_the_thread_is_not_the_answer(client, hub, hub_db, github):
        """In a Work chat each run of an agent answers in the same thread: the answer of a run is its own message."""
        done = asked_and_answered(client, hub, hub_db, github, "The first answer.")
        m, c, r = tables.chat_messages, tables.chats, tables.runs
        query = select(m.c.chat_id, m.c.thread_root_id, m.c.author_profile_id).where(m.c.body == "The first answer.")
        (chat_id, root, profile) = sql(hub_db, query)[0]
        (other,) = sql(hub_db, select(r.c.id).where(r.c.id != done["child"]).order_by(r.c.id.desc()).limit(1))[0]
        bump = c.update().values(event_seq=c.c.event_seq + 1).where(c.c.id == chat_id).returning(c.c.event_seq)
        ((seq,),) = sql(hub_db, bump)
        later = {"chat_id": chat_id, "seq": seq, "thread_root_id": root, "author_profile_id": profile, "label": []}
        sql(hub_db, m.insert().values({**later, "run_id": other, "body": "A later run's words."}))
        found = client.portal.call(_last_reply, client.app.state.engine, done["child"])
        assert found is not None and found[1] == "The first answer."

    async def _last_reply(engine, run_id: int):
        async with engine.connect() as conn:
            return await agent_asks.last_reply(conn, run_id)
