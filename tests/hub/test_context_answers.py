"""The confirmed answers in the prompt of a run (plan active-context-answers, criteria a1, a2, a3 and a4;
``evo_agents.hub.server.context_answers``).

Checked on the hub and the runs of ``tests.hub.test_memory_agents`` (project aud, alice a writer who dispatches the runs;
an author run works in the repo aud-harness), with decisions written straight into the table as answered:

- a1: with ``context.answers`` set, the prompt ends with a block of the answered decisions and feedback memories whose
  cited paths match the run (path before directory before repo), best match first, at most 4 KiB, each line naming its
  source; an answer that matches nothing is not there, and no match leaves the prompt as it was; the block comes before
  the index of an agent's memories;
- a2: never in the block: a decision of another project, of category credential, wait, release or loop, one whose run
  showed an audience the run's agent does not hold; a memory whose label the agent's sink does not reach, whose audience
  the agent does not hold, someone else's feedback;
- a3: each answer loaded is a row of context_answer_loads, once for a run claimed again, and counted by day and week;
- a4: the key is false by default, the policy revision that sets it is listed, and with it off the prompt of every run is
  as it was, byte for byte.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.server import context_answers
from evo_agents.hub.server.runs.service import claims
from tests.hub import live
from tests.hub.test_kg_audience import A, B, done, on_hub, user_id
from tests.hub.test_memory_agents import (  # noqa: F401 (hub and agents are fixtures)
    a_run,
    agents,
    body,
    hub,
)

NOW = datetime.now(UTC)
REPO = f"{A}-harness"


def prompt(hub, run_id: int) -> str:
    return on_hub(hub, lambda conn: claims._run_spec(conn, run_id)).prompt


def policy(hub, **context) -> dict:
    return done(hub.client.put(f"/v1/projects/{A}/policy", json={"policy": {"context": context}}, headers=hub.admin))


def turn_on(hub) -> dict:
    return policy(hub, answers=True)


def project_id(hub, name: str = A) -> int:
    return live.sql(hub.db, select(tables.projects.c.id).where(tables.projects.c.name == name))[0][0]


def decide(hub, run_id: int, question: str, cites: str, *, category="architecture", day=1, project=A, **more) -> int:
    row = {
        "run_id": run_id,
        "project_id": project_id(hub, project),
        "category": category,
        "question": question,
        "context": f"See code:{cites}.",
        "options": [{"key": "keep", "label": "Keep it"}, {"key": "drop", "label": "Drop it"}],
        "state": "answered",
        "answer_option": "keep",
        "answer_text": f"Keep it, said on day {day}.",
        "answered_by": user_id(hub, "alice"),
        "asked_at": NOW - timedelta(days=day, hours=1),
        "answered_at": NOW - timedelta(days=day),
    }
    return live.sql(hub.db, insert(tables.decisions).values({**row, **more}).returning(tables.decisions.c.id))[0][0]


def feedback(hub, name: str, cites: str, who: str = "alice") -> dict:
    text = f"---\nname: {name}\ndescription: {name} says use code:{cites}\nmetadata:\n  type: feedback\n---\nText.\n"
    memory = {"scope": "project", "project": A, "location": "harness", "name": f"{name}.md", "type": "feedback"}
    return done(hub.client.put("/v1/memories", json={**memory, "body": text}, headers=getattr(hub, who)))


def loads(hub) -> list[tuple]:
    c = tables.context_answer_loads
    return live.sql(hub.db, select(c.c.run_id, c.c.source, c.c.source_id, c.c.scope_key).order_by(c.c.id))


def block_of(text: str) -> str:
    return text.partition("# Confirmed answers\n")[2]


@pytest.fixture
def run(hub, agents):  # noqa: F811
    return a_run(hub, agents.studio, "alice")


# a4: the key


def test_context_answers_is_false_by_default_and_its_revision_is_listed(hub, agents):  # noqa: F811
    shown = done(hub.client.get(f"/v1/projects/{A}/policy", headers=hub.admin))
    assert shown["effective"]["context"] == {"answers": False} and shown["revision"] == 0
    turned = turn_on(hub)
    assert turned["effective"]["context"]["answers"] is True and turned["revision"] == 1
    listed = done(hub.client.get(f"/v1/projects/{A}/policy/revisions", headers=hub.admin))
    assert [r["revision"] for r in listed] == [1]
    bad = hub.client.put(
        f"/v1/projects/{A}/policy", json={"policy": {"context": {"answers": "yes"}}}, headers=hub.admin
    )
    assert bad.status_code == 422


def test_answers_off_leaves_the_prompt_of_every_run_as_it_was(hub, agents, run):  # noqa: F811
    before = prompt(hub, run)
    decide(hub, run, "Where does the policy live?", f"{REPO}:docs/hub.md")
    feedback(hub, "orbit-rule", f"{REPO}:docs/hub.md")
    assert prompt(hub, run) == before and "# Confirmed answers" not in before  # the key is off
    policy(hub, answers=False)
    assert prompt(hub, run) == before
    turn_on(hub)
    on = prompt(hub, run)
    assert on.startswith(before.rstrip("\n")) and on != before
    policy(hub, answers=False)
    assert prompt(hub, run) == before  # off again: as it was, byte for byte
    assert len(loads(hub)) == 2 and {row[1] for row in loads(hub)} == {"decision", "memory"}


# a1: the block


def test_the_block_holds_the_answers_that_match_the_run_best_first(hub, agents, run):  # noqa: F811
    turn_on(hub)
    plain = prompt(hub, run)
    assert "# Confirmed answers" not in plain  # nothing answered yet: the prompt as it was
    by_repo = decide(hub, run, "Which style?", f"{REPO}:src/other.py", day=3)
    other_repo = decide(hub, run, "Which tool?", "elsewhere:src/other.py", day=2)
    nothing = decide(hub, run, "No path here", "none", day=1, context=None)
    older = decide(hub, run, "Old on the same repo", f"{REPO}:src/z.py", day=9)
    text = prompt(hub, run)
    block = block_of(text)
    assert f"decision {by_repo}" in block and f"decision {older}" in block
    assert f"decision {other_repo}" not in block and f"decision {nothing}" not in block
    assert block.index(f"decision {by_repo}") < block.index(f"decision {older}")  # newest of equal match first
    assert "Keep it: Keep it, said on day 3." in block and "evo-agents hub decision show" in block
    assert "(architecture, answered" in block and " by alice)" in block

    memory = feedback(hub, "orbit-rule", f"{REPO}:docs/hub.md")
    block = block_of(prompt(hub, run))
    assert f"memory {memory['id']}" in block and f"memory_get {memory['id']}" in block
    assert block.count("\n- ") == 3


def test_a_path_the_run_names_ranks_before_a_directory_before_the_repo(hub, agents, run):  # noqa: F811
    turn_on(hub)
    on_hub(
        hub,
        lambda conn: conn.execute(
            update(tables.runs).where(tables.runs.c.id == run).values(request="Edit src/app/main.py now.")
        ),
    )
    repo = decide(hub, run, "repo level", f"{REPO}:docs/x.md", day=1)
    directory = decide(hub, run, "dir level", f"{REPO}:src/app/other.py", day=5)
    path = decide(hub, run, "path level", f"{REPO}:src/app/main.py", day=9)
    block = block_of(prompt(hub, run))
    order = [block.index(f"decision {n}") for n in (path, directory, repo)]
    assert order == sorted(order)
    assert [row[3] for row in loads(hub)] == [
        f"path:{REPO}:src/app/main.py",
        f"dir:{REPO}:src/app",
        f"repo:{REPO}",
    ]


def test_the_block_is_at_most_4_kib_and_comes_before_the_index_of_memories(hub, agents):  # noqa: F811
    turn_on(hub)
    scout = a_run(hub, agents.studio, "alice", "scout")
    for number in range(40):
        decide(hub, scout, f"Question {number} " + "words " * 40, f"{REPO}:src/m{number}.py", day=number + 1)
    done(
        hub.client.put(
            "/v1/memories",
            json={
                "scope": "project",
                "project": A,
                "location": "harness",
                "name": "orbit-scout.md",
                "type": "project",
                "agent": "scout",
                "body": body("orbit-scout"),
            },
            headers=hub.alice,
        )
    )
    text = prompt(hub, scout)
    block = block_of(text).partition("# Memories of @scout")[0]
    assert 0 < len(block.encode()) <= context_answers.BLOCK_BYTES
    assert text.index("# Confirmed answers") < text.index("# Memories of @scout")
    assert "decision" in block and "Question 0 " in block and "Question 39 " not in block  # the newest as many as fit


# a2: what never loads


def test_project_category_and_clearance_keep_an_answer_out_of_the_block(hub, agents, run):  # noqa: F811
    turn_on(hub)
    cites = f"{REPO}:src/app/main.py"
    kept = decide(hub, run, "kept", cites)
    stranger = decide(hub, run, "another project", cites, project=B)
    banned = {
        c: decide(hub, run, f"category {c}", cites, category=c) for c in ("credential", "wait", "release", "loop")
    }
    other_run = a_run(hub, agents.studio, "alice")
    on_hub(
        hub,
        lambda conn: conn.execute(
            update(tables.runs).where(tables.runs.c.id == other_run).values(seen_audience=["chat:999999"])
        ),
    )
    hidden = decide(hub, other_run, "after the note", cites)
    shown = decide(hub, a_run(hub, agents.studio, "alice"), "asked by a run that showed nothing", cites)
    block = block_of(prompt(hub, run))
    assert f"decision {kept} " in block and f"decision {shown} " in block
    for number in (stranger, hidden, *banned.values()):
        assert f"decision {number} " not in block


def test_clearance_keeps_a_memory_the_agent_may_not_read_out_of_the_block(hub, agents, run):  # noqa: F811
    turn_on(hub)
    cites = f"{REPO}:src/app/main.py"
    open_one = feedback(hub, "open-rule", cites)["id"]
    high = feedback(hub, "high-rule", cites)["id"]
    chatty = feedback(hub, "chat-rule", cites)["id"]
    bobs = feedback(hub, "bobs-rule", cites, who="alice")["id"]
    m = tables.memories
    live.sql(hub.db, update(m).where(m.c.id == high).values(label={"level": "customer", "projects": [A]}))
    live.sql(
        hub.db,
        update(m)
        .where(m.c.id == chatty)
        .values(label={"level": "internal", "projects": [A], "audience": ["chat:999999"]}),
    )
    live.sql(hub.db, update(m).where(m.c.id == bobs).values(owner_id=user_id(hub, "bob")))
    block = block_of(prompt(hub, run))
    assert f"memory {open_one} " in block
    for number in (high, chatty, bobs):
        assert f"memory {number} " not in block
    live.sql(hub.db, update(m).where(m.c.id == open_one).values(archived_at=NOW))
    assert "# Confirmed answers" not in prompt(hub, run)


# a3: the loads


def test_each_answer_loaded_is_recorded_once_and_counted_by_day_and_week(hub, agents, run):  # noqa: F811
    turn_on(hub)
    cites = f"{REPO}:src/app/main.py"
    first = decide(hub, run, "first", cites)
    memory = feedback(hub, "orbit-rule", cites)["id"]
    once = prompt(hub, run)
    assert prompt(hub, run) == once  # claimed again: the same block, and no second row
    assert sorted(loads(hub)) == [(run, "decision", first, f"repo:{REPO}"), (run, "memory", memory, f"repo:{REPO}")]
    second = a_run(hub, agents.studio, "alice")
    prompt(hub, second)
    assert len(loads(hub)) == 4

    start = NOW - timedelta(days=1)
    end = NOW + timedelta(days=1)
    pid = project_id(hub)
    days = on_hub(hub, lambda conn: context_answers.load_counts(conn, pid, start, end, "day"))
    assert [(d.runs, d.answers) for d in days] == [(2, 4)]
    weeks = on_hub(hub, lambda conn: context_answers.load_counts(conn, pid, start - timedelta(days=14), end, "week"))
    assert sum(w.answers for w in weeks) == 4 and sum(w.runs for w in weeks) >= 2
    assert on_hub(hub, lambda conn: context_answers.load_counts(conn, project_id(hub, B), start, end)) == []
    with pytest.raises(ValueError):
        on_hub(hub, lambda conn: context_answers.load_counts(conn, pid, start, end, "month"))
