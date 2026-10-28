"""The labels of the knowledge graph's searches on the hub (``evo_agents.hub.kg_labels``, schema 0177) and the night's
signal kg.query.grep_after_rate.

Step 1 of plan kg-retrieval-quality, criterion a1: each kg_search of a session becomes a query label kept on the hub
with the session's label, read back only through a sink whose clearance reads that label; a facet made again, as when
FACET_VERSION changes, replaces the labels before it; and the night's figures push the share of the chains of lookups a
grep followed as a signal, once a night."""

from datetime import date
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.hub import facets, kg_labels, tables
from evo_agents.hub.server.curator import _owner_access
from evo_agents.hub.server.kg_labels import visible_labels
from tests.hub.live import sql
from tests.hub.test_digest import answered, result, session_lines, tool_use
from tests.hub.test_facet_job import SESSION, analyzed, later, session_facet_of, user_of
from tests.hub.test_runs import OWNER, PROJECT
from tests.hub.test_session_transcripts import hub, parts_of, send, statuses, transcript  # noqa: F401 (hub: a fixture)

KG = "mcp__plugin_evo-hub_evo-hub__kg_search"
FOUND = """2 result(s) for 'claim a job' in project evo-agents (build 3)
[app:app/queue.py::Queue.claim] Symbol 'Queue.claim' (parsed) line=12
[app:docs/queue.md#claiming] Section 'Claiming' (parsed)"""


def kg_lines() -> list[dict]:
    """session_lines, then a kg_search, the file of its first result read, a grep and a file no result named."""
    return [
        *session_lines(),
        answered("msg_k1", 16, tool_use("toolu_k1", KG, query="claim a job")),
        result("toolu_k1", 17, FOUND),
        answered("msg_k2", 18, tool_use("toolu_k2", "Read", file_path="/work/app/app/queue.py")),
        result("toolu_k2", 19, "class Queue: ..."),
        answered("msg_k3", 20, tool_use("toolu_k3", KG, query="lease")),
        result("toolu_k3", 21, "0 result(s) for 'lease' in project evo-agents (build 3)"),
        answered("msg_k4", 22, tool_use("toolu_k4", "Bash", command="rg -n lease /work/app")),
        result("toolu_k4", 23, "app/lease.py:3: def lease"),
        answered("msg_k5", 24, tool_use("toolu_k5", "Bash", command="sed -n 1,40p /work/app/app/lease.py")),
        result("toolu_k5", 25, "def lease(): ..."),
    ]


def stored_labels(db) -> list[dict]:
    t = tables.kg_query_labels
    columns = [t.c[name] for name in ("kind", "session_id", "run_id", "seq", "query", "results", "clicks")]
    columns += [t.c[name] for name in ("grep_after", "after_grep", "label", "searched_at")]
    rows = sql(db, select(*columns).order_by(t.c.seq))
    return [dict(zip([column.name for column in columns], row, strict=True)) for row in rows]


def read_through(hub, sink: str) -> list[dict]:
    async def read():
        async with hub.client.app.state.engine.connect() as conn:
            access = await _owner_access(conn, user_of(hub.db, OWNER), OWNER, PROJECT)
            return await visible_labels(conn, access, sink)

    return hub.client.portal.call(read)


def test_a_sessions_searches_are_kept_under_its_label_and_read_only_through_a_sink_that_clears_it(hub, monkeypatch):
    path = transcript(hub.tmp_path, kg_lines())
    assert statuses(send(hub, "owner", parts_of(hub, path))) == ["stored"]
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}
    body = session_facet_of(hub.db)["body"]
    assert kg_labels.KEY not in body, "the queries stay under the label, not in the facet's body"
    assert (body["kg"]["queries"], body["kg"]["clicked"], body["kg"]["grep_after"]) == (1, 1, 1)
    first, empty = stored_labels(hub.db)
    assert (first["kind"], first["session_id"], first["run_id"], first["query"]) == (
        "session",
        SESSION,
        None,
        "claim a job",
    )
    assert [(item["id"], item["rank"], item["path"]) for item in first["results"]] == [
        ("app:app/queue.py::Queue.claim", 1, "app/queue.py"),
        ("app:docs/queue.md#claiming", 2, "docs/queue.md"),
    ]
    assert first["clicks"] == [
        {"id": "app:app/queue.py::Queue.claim", "path": "app/queue.py", "rank": 1, "how": "read"}
    ]
    assert first["label"]["level"] == "internal" and first["searched_at"] is not None
    assert (empty["query"], empty["results"], empty["grep_after"]) == ("lease", [], True)
    assert empty["after_grep"] == ["/work/app/app/lease.py"], "read through Bash after the grep"

    assert [item["query"] for item in read_through(hub, "claude-code@anthropic")] == ["lease", "claim a job"]
    assert read_through(hub, "codex@openai") == [], "a public sink reads no internal label"

    monkeypatch.setattr(facets, "FACET_VERSION", facets.FACET_VERSION + 1)
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}, "an older facet is made again"
    again = stored_labels(hub.db)
    assert [item["query"] for item in again] == ["claim a job", "lease"], "in place of the labels before"
    assert sql(hub.db, select(func.count()).select_from(tables.kg_query_labels))[0][0] == 2


def test_the_nights_figures_push_the_share_of_chains_a_grep_followed_once_a_night(hub):
    owner = user_of(hub.db, OWNER)
    project = sql(hub.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    figures = {"kg_health": {"retrieval": {"chains": 4, "grep_after": 1, "grep_after_rate": 0.25}}}

    async def push(figures, night=date(2026, 10, 28)):
        async with hub.client.app.state.engine.begin() as conn:
            return await kg_labels.push_grep_after(conn, project, owner, night, figures)

    call = partial(hub.client.portal.call, push)
    assert call(figures) is True
    assert call(figures) is False, "the same night again adds nothing"
    assert call({"kg_health": {"retrieval": {"chains": 0, "grep_after_rate": 0.0}}}) is False, "no chain: no signal"
    assert call({"kg_health": None}) is False
    s, k = tables.signals, tables.signal_keys
    rows = sql(
        hub.db,
        select(k.c.name, k.c.kind, k.c.unit, s.c.value, s.c.via, s.c.user_id, s.c.external_id).select_from(
            s.join(k, k.c.id == s.c.key_id)
        ),
    )
    assert rows == [
        (
            kg_labels.SIGNAL_KEY,
            "number",
            "percent",
            25.0,
            "member",
            owner,
            "collect:kg.query.grep_after_rate:2026-10-28",
        )
    ]
    assert call({**figures, "kg_health": {"retrieval": {"chains": 2, "grep_after_rate": 0.5}}}, date(2026, 10, 29))
    assert sql(hub.db, select(func.count()).select_from(s))[0][0] == 2, "one signal a night"
