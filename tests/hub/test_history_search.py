"""Reading a project's whole history on the hub (``evo_agents.hub.history_index``, ``evo_agents.hub.server.history_search``,
``evo_agents.hub.server.history_mcp``) and schema 0057, on a hub with a blob store: test_history_runs' lab, the
owner an admin of evo-agents with a charter whose worker on duty runs every kind of run.

Step 5 of plan history-driven-curator, criteria a1 (its part of runs, local sessions and group chats), a4 and a13: the
words of every transcript part and of every run that ended indexed once, in Postgres, and searched with Postgres
full-text search; transcripts, runs and the group chats the caller may read, as the hub's routes read them; the
sessions and runs of the whole history listed by time, member, runtime, outcome and cluster, the numbers first; the
lines of a transcript around one; the index gone with a transcript's content; the hub's MCP tools of the history for
the agent of a review run; the command line; only additions to the contract."""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import history_index, mcp_tools, tables
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_chats import created as chat_created
from tests.hub.test_chats import sent as chat_sent
from tests.hub.test_curator import NIGHT
from tests.hub.test_digest import FAILED_TEST, said, session_lines
from tests.hub.test_facet_job import analyzed, ended_local_run, later
from tests.hub.test_history_runs import FIRST, SECOND, lab, upload  # noqa: F401 (lab is a fixture)
from tests.hub.test_migrate import INFO_TABLES as INFO
from tests.hub.test_migrate import move_to
from tests.hub.test_plans import registration
from tests.hub.test_review_runs import collected
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import OTHER, OWNER, PROJECT, claim, moved
from tests.hub.test_session_digests import SINKS
from tests.hub.test_session_transcripts import parts_of, records, send, statuses, transcript

MCP_HEADERS = {"Accept": "application/json", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
HISTORY_TOOLS = [
    "history_sessions",
    "history_search",
    "transcript_lines",
    "history_clusters",
    "curator_findings",
    "curator_playbook",
]


def indexed(client, **options) -> dict:
    state = client.app.state
    return client.portal.call(partial(history_index.index_history, state.engine, state.blobs, **options))


def rows_of(db, *where) -> list:
    lines = tables.history_lines
    query = select(lines.c.source, lines.c.session_id, lines.c.run_id, lines.c.line, lines.c.role, lines.c.text)
    return records(db, query.where(*where).order_by(lines.c.source, lines.c.session_id, lines.c.run_id, lines.c.line))


def search(lab, who: str = "owner", **params) -> dict:
    response = lab.client.get(f"/v1/projects/{PROJECT}/curator/history/search", params=params, headers=lab.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def listed(lab, who: str = "owner", **params) -> dict:
    path = f"/v1/projects/{PROJECT}/curator/history/sessions"
    response = lab.client.get(path, params=params, headers=lab.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def lines_of(lab, session: str, who: str = "owner", **params):
    path = f"/v1/projects/{PROJECT}/transcripts/{session}/lines"
    return lab.client.get(path, params=params, headers=lab.headers[who])


def two_sessions(lab) -> None:
    """The owner's session FIRST (a failed test, a rejected push) and SECOND (a correction), indexed."""
    upload(lab, FIRST)
    upload(lab, SECOND, [*session_lines("Write the docs of the queue."), said("không, viết ngắn thôi", 20)])
    assert indexed(lab.client) == {"rows": 27, "transcripts": 2}


ROLES = [
    (2, "user"),
    (3, "agent"),
    (4, "call Bash"),
    (5, "Bash failed"),
    (6, "call Read"),
    (7, "Read result"),
    (8, "call Edit"),
    (9, "Edit result"),
    (10, "call Bash"),
    (11, "Bash result"),
    (12, "call Bash"),
    (13, "Bash failed"),
    (14, "system"),
]  # the entries of session_lines(): the caveat, the snapshot and the command name have none


def held_review(lab) -> int:
    """The night's review run, claimed and running on the worker on duty; its id."""
    assert collected(lab.client, NIGHT)["review"] == 1
    spec = claim(lab.client, lab.worker)
    assert spec["kind"] == "review"
    moved(lab.client, lab.worker, spec["id"], "running")
    return spec["id"]


def agent_call(lab, run_id: int, name: str, arguments: dict | None = None, sink: str | None = None) -> dict:
    message = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}, "_meta": {"evo-agents/data": True}},
    }
    headers = {**lab.worker["headers"], **MCP_HEADERS, mcp_tools.RUN_HEADER: str(run_id)}
    if sink:
        headers[mcp_tools.SINK_HEADER] = sink
    response = lab.client.post("/mcp", json=message, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def text_of(result: dict) -> str:
    assert not result.get("isError"), result
    return result["content"][0]["text"]


# The index


def test_each_part_of_a_transcript_is_indexed_once_and_a_new_part_alone_is_read(lab):
    two_sessions(lab)
    first = rows_of(lab.db, tables.history_lines.c.session_id == FIRST)
    assert [(row.line, row.role) for row in first] == ROLES
    assert first[3].text == FAILED_TEST and first[11].text == "rejected: non-fast-forward"
    assert {row.source for row in first} == {"transcript"}
    assert indexed(lab.client) == {}, "nothing new: no part is read again"

    path = transcript(lab.tmp_path, [*session_lines(), said("chạy lại pytest với -x", 30)], name=f"{FIRST}.jsonl")
    held = lab.client.get(f"/v1/projects/{PROJECT}/transcripts/{FIRST}", headers=lab.headers["owner"]).json()
    end = held["transcripts"][0]["source_bytes"]
    assert statuses(send(lab, "owner", parts_of(lab, path, start=end, line=17, session=FIRST))) == ["stored"]
    assert indexed(lab.client) == {"rows": 1, "transcripts": 1}
    (added,) = rows_of(lab.db, tables.history_lines.c.session_id == FIRST, tables.history_lines.c.line == 17)
    assert (added.role, added.text) == ("user", "chạy lại pytest với -x")
    h = tables.history_indexed
    progress = sql(lab.db, select(h.c.transcript_id, h.c.parts, h.c.lines).order_by(h.c.transcript_id))
    assert sorted((parts, lines) for _, parts, lines in progress) == [(1, 14), (2, 14)]


def test_a_run_that_ended_is_indexed_from_its_events_and_again_when_more_came(lab):
    run_id = ended_local_run(lab.client, lab.headers["owner"], "0199a3c1-0000-7000-8000-00000000c001")
    assert indexed(lab.client) == {}, "a run that ended a moment ago waits until its last events have come"
    assert indexed(lab.client, now=later(lab.db)) == {"rows": 2, "runs": 1}
    found = rows_of(lab.db, tables.history_lines.c.run_id == run_id)
    assert [(row.role, row.text) for row in found] == [
        ("call Bash", "curl hub"),
        ("tool failed", "curl: connection refused"),
    ]
    assert found[1].line == found[0].line + 1  # the event's seq on the hub; a chunk of the agent's message carries
    # its words in content.text, which these events lack
    assert indexed(lab.client, now=later(lab.db)) == {}, "its fingerprint did not change"
    r = tables.runs
    sql(lab.db, update(r).values(event_seq=r.c.event_seq + 1).where(r.c.id == run_id))
    assert indexed(lab.client, now=later(lab.db)) == {"rows": 2, "runs": 1}, "written again, never twice"
    assert len(rows_of(lab.db, tables.history_lines.c.run_id == run_id)) == 2


def test_deleting_a_transcripts_content_deletes_its_words(lab):
    two_sessions(lab)
    deleted = lab.client.delete(f"/v1/projects/{PROJECT}/transcripts/{FIRST}", headers=lab.headers["admin"])
    assert deleted.status_code == 403  # a hub admin without a grant: the project's admin deletes it
    deleted = lab.client.delete(f"/v1/projects/{PROJECT}/transcripts/{FIRST}", headers=lab.headers["owner"])
    assert deleted.status_code == 200, deleted.text
    assert rows_of(lab.db, tables.history_lines.c.session_id == FIRST) == []
    assert len(rows_of(lab.db, tables.history_lines.c.session_id == SECOND)) == 14
    assert indexed(lab.client) == {}, "a tombstone has nothing to index"
    assert search(lab, q="AssertionError")["total"] == 1


# Search


def test_search_finds_words_of_transcripts_runs_and_chats_with_the_evidence_that_names_them(lab):
    two_sessions(lab)
    found = search(lab, q="AssertionError")
    assert found["total"] == 2 and found["sources"] == ["transcript", "run", "chat"]
    assert {hit["evidence"] for hit in found["hits"]} == {f"transcript:{FIRST}:5", f"transcript:{SECOND}:5"}
    hit = found["hits"][0]
    assert (hit["source"], hit["role"], hit["login"], hit["runtime"], hit["line"]) == (
        "transcript",
        "Bash failed",
        OWNER,
        "claude-code",
        5,
    )
    assert "«AssertionError»" in hit["text"]
    assert search(lab, q='"viết ngắn"')["hits"][0]["evidence"] == f"transcript:{SECOND}:17"
    assert search(lab, q="AssertionError -assert")["total"] == 0
    assert search(lab, q="AssertionError", until="2026-10-08")["total"] == 0  # the lines are of 2026-10-08, 01:00
    assert search(lab, q="AssertionError", since="2026-10-08", until="2026-10-09")["total"] == 2
    assert search(lab, q="AssertionError", login=OTHER)["total"] == 0
    assert search(lab, q="AssertionError", runtime="codex")["total"] == 0
    assert search(lab, "other", q="AssertionError")["total"] == 2  # a writer of the project reads them by label
    assert search(lab, q="AssertionError", limit=1, offset=1)["hits"][0]["evidence"] in {
        f"transcript:{FIRST}:5",
        f"transcript:{SECOND}:5",
    }
    refused = lab.client.get(
        f"/v1/projects/{PROJECT}/curator/history/search", params={"q": "x"}, headers=lab.headers["stranger"]
    )
    assert refused.status_code == 404

    mine = ended_local_run(lab.client, lab.headers["owner"], "0199a3c1-0000-7000-8000-00000000c002", "dial tcp mine")
    theirs = ended_local_run(lab.client, lab.headers["other"], "0199a3c1-0000-7000-8000-00000000c003", "dial tcp yours")
    assert indexed(lab.client, now=later(lab.db))["runs"] == 2
    owners = search(lab, q="dial", source=["run"])
    assert [hit["run_id"] for hit in owners["hits"]] == [mine]  # a local run is its member's alone
    assert owners["hits"][0]["evidence"] == f"run:{mine}:{owners['hits'][0]['line']}"
    assert [hit["run_id"] for hit in search(lab, "other", q="dial", source="run")["hits"]] == [theirs]
    assert owners["hits"][0]["run_kind"] == "local" and owners["hits"][0]["role"] == "tool failed"

    chat = chat_created(lab.client, lab.headers["owner"], "Night talk", people=[OTHER], agents=[PROJECT])
    chat_sent(lab.client, lab.headers["other"], chat["id"], "the review keeps hitting a dial timeout")
    other_chat = chat_created(lab.client, lab.headers["reader"], "Readers only", people=[])
    chat_sent(lab.client, lab.headers["reader"], other_chat["id"], "dial timeout, readers only")
    talked = search(lab, q="dial timeout", source="chat")
    assert [(hit["source"], hit["chat"], hit["login"], hit["role"]) for hit in talked["hits"]] == [
        ("chat", "Night talk", OTHER, "person")
    ]  # not a chat the owner is not in, nor one whose messages name no project
    assert talked["hits"][0]["evidence"] is None and talked["hits"][0]["line"] >= 1
    assert search(lab, "reader", q="dial timeout", source="chat")["total"] == 0  # not in Night talk
    assert search(lab, q="dial timeout", source="chat", runtime="claude-code")["total"] == 0  # a chat has no runtime


# The sessions and runs


def test_the_sessions_and_runs_of_the_history_come_with_the_numbers_of_their_facets(lab):
    two_sessions(lab)
    run_id = ended_local_run(lab.client, lab.headers["owner"], "0199a3c1-0000-7000-8000-00000000c004")
    assert analyzed(lab.client, later(lab.db)) == {"events": 1, "transcript": 2}
    found = listed(lab)
    assert found["total"] == 3
    by = {item["evidence"]: item for item in found["items"]}
    first = by[f"session:{FIRST}"]
    assert (first["kind"], first["login"], first["runtime"], first["transcript"], first["source"]) == (
        "session",
        OWNER,
        "claude-code",
        True,
        "transcript",
    )
    assert first["numbers"]["tool_errors"] == 2 and first["numbers"]["messages"] > 0
    assert first["summary"] is None and first["cluster"] is None
    run = by[f"run:{run_id}:1"]
    assert (run["kind"], run["run_kind"], run["state"], run["transcript"]) == ("run", "local", "ended", False)
    assert listed(lab, kind="run")["total"] == 1 and listed(lab, kind="session")["total"] == 2
    assert listed(lab, "other")["total"] == 2, "another member's local run is not theirs"
    assert listed(lab, login=OTHER)["total"] == 0
    assert listed(lab, outcome="ended")["total"] == 1, "a run's state counts as its outcome"

    f = tables.session_facets
    llm = {"summary": "Fixed the claim.", "task_type": "fix", "outcome": "failed", "failure_modes": ["FM-3.2"]}
    sql(lab.db, update(f).values(llm=llm, llm_version=1).where(f.c.session_id == FIRST, f.c.kind == "session"))
    sql(lab.db, tables.facet_clusters.insert().values(project_id=1, key="fix/FM-3.2", sessions=1))
    failed = listed(lab, outcome="failed")
    assert [item["evidence"] for item in failed["items"]] == [f"session:{FIRST}"]
    assert failed["items"][0]["cluster"] == "fix/FM-3.2" and failed["items"][0]["failure_modes"] == ["FM-3.2"]
    assert listed(lab, cluster="fix/FM-3.2")["total"] == 1
    sql(lab.db, tables.facet_clusters.insert().values(project_id=1, key="fix/none", merged_into="fix/FM-3.2"))
    sql(lab.db, update(f).values(llm={**llm, "failure_modes": []}).where(f.c.session_id == SECOND))
    assert listed(lab, cluster="fix/FM-3.2")["total"] == 2, "a group the model joined counts in the cluster"
    assert listed(lab, since="2030-01-01")["total"] == 0
    assert listed(lab, limit=1)["items"][0]["evidence"] in by and listed(lab, limit=1)["total"] == 3


# The lines of a transcript


def test_the_lines_of_a_transcript_around_one_come_as_its_entries(lab):
    two_sessions(lab)
    response = lines_of(lab, FIRST, line=5, before=2, after=1)
    assert response.status_code == 200, response.text
    found = response.json()
    assert (found["first"], found["last"], found["lines"], found["evidence"]) == (3, 6, 16, f"transcript:{FIRST}:5")
    assert [(entry["line"], entry["role"]) for entry in found["entries"]] == [
        (3, "agent"),
        (4, "call Bash"),
        (5, "Bash failed"),
        (6, "call Read"),
    ]
    assert found["entries"][2]["text"] == FAILED_TEST
    quiet = lines_of(lab, FIRST, line=15, before=0, after=0).json()
    assert quiet["entries"] == [{"line": 15, "role": "file-history-snapshot", "text": ""}]
    assert lines_of(lab, FIRST, line=99).status_code == 422
    assert lines_of(lab, FIRST, line=1, file="subagents/agent-x.jsonl").status_code == 404
    assert lines_of(lab, "0199b1c2-7d3e-7a10-9c41-00000000ffff", line=1).status_code == 404
    assert lines_of(lab, FIRST, "stranger", line=1).status_code == 404


# The tools of the hub's MCP endpoint


def test_the_agent_of_a_review_run_reads_the_history_through_the_hubs_tools(lab):
    two_sessions(lab)
    assert analyzed(lab.client, later(lab.db)) == {"transcript": 2}
    run_id = held_review(lab)
    headers = {**lab.worker["headers"], **MCP_HEADERS, mcp_tools.RUN_HEADER: str(run_id)}
    listing = lab.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
    names = [tool["name"] for tool in listing.json()["result"]["tools"]]
    assert names[-6:] == HISTORY_TOOLS

    found = agent_call(lab, run_id, "history_search", {"query": "AssertionError"})
    assert "2 of 2 hits of 'AssertionError'" in text_of(found) and f"transcript:{FIRST}:5" in text_of(found)
    assert found["structuredContent"]["hits"][0]["source"] == "transcript"
    sessions = text_of(agent_call(lab, run_id, "history_sessions", {"kind": "session"}))
    assert sessions.startswith("2 of 2 sessions and runs of project evo-agents") and "2 tool errors" in sessions
    around = text_of(agent_call(lab, run_id, "transcript_lines", {"session_id": FIRST, "line": 5, "before": 1}))
    assert f"> L5 Bash failed: {FAILED_TEST}" in around and "  L4 call Bash" in around
    clusters = text_of(agent_call(lab, run_id, "history_clusters", {}))
    assert clusters.startswith("0 clusters of 3 sessions or more in project evo-agents")
    refused = agent_call(lab, run_id, "history_sessions", {"since": "2026-13-01"})
    assert refused["isError"] is True and "is not a day" in refused["content"][0]["text"]
    bad = agent_call(lab, run_id, "transcript_lines", {"session_id": FIRST})
    assert bad["isError"] is True and "bad arguments for transcript_lines" in bad["content"][0]["text"]


def member_call(lab, name: str, arguments: dict, sink: str | None = None) -> dict:
    """A tool call of the owner's own session (its machine token, no run), through ``sink`` when given."""
    message = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    headers = {**lab.headers["owner"], **MCP_HEADERS, mcp_tools.PROJECT_HEADER: PROJECT}
    if sink:
        headers[mcp_tools.SINK_HEADER] = sink
    response = lab.client.post("/mcp", json=message, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def night_talk(lab) -> dict:
    """Night talk, the owner's chat with someone-else and the project's agent, where someone-else wrote of a dial
    timeout; the chat."""
    chat = chat_created(lab.client, lab.headers["owner"], "Night talk", people=[OTHER], agents=[PROJECT])
    chat_sent(lab.client, lab.headers["other"], chat["id"], "the review keeps hitting a dial timeout")
    return chat


def test_the_reviewer_alone_of_the_agents_of_runs_searches_the_owners_chats_and_local_runs(lab):
    two_sessions(lab)
    chat = night_talk(lab)
    mine = ended_local_run(lab.client, lab.headers["owner"], "0199a3c1-0000-7000-8000-00000000c005", "dial tcp mine")
    assert indexed(lab.client, now=later(lab.db))["runs"] == 1
    assert analyzed(lab.client, later(lab.db))["events"] == 1
    owners = search(lab, q="dial")  # the owner's own session reads the chat it is in and its local run
    assert sorted((hit["source"], hit["run_id"]) for hit in owners["hits"]) == [("chat", None), ("run", mine)]

    review = held_review(lab)  # its agent is the owner, the charter's, with the run's scope
    found = agent_call(lab, review, "history_search", {"query": "dial"})["structuredContent"]
    assert found["sources"] == ["transcript", "run", "chat"], "the Reviewer reads what the charter's owner reads"
    assert sorted((hit["source"], hit["run_id"]) for hit in found["hits"]) == [("chat", None), ("run", mine)]
    talked = agent_call(lab, review, "history_search", {"query": "dial", "sources": ["chat"]})["structuredContent"]
    assert [(hit["source"], hit["chat"], hit["evidence"]) for hit in talked["hits"]] == [("chat", "Night talk", None)]
    assert not agent_call(lab, review, "run_events", {"run_id": mine}).get("isError")
    moved(lab.client, lab.worker, review, "verifying", "done")

    chat_sent(lab.client, lab.headers["owner"], chat["id"], f"@{PROJECT} what does the history say of it?")
    spec = claim(lab.client, lab.worker)
    assert spec["kind"] == "chat"
    moved(lab.client, lab.worker, spec["id"], "running")
    talk = agent_call(lab, spec["id"], "history_search", {"query": "dial"})["structuredContent"]
    assert (talk["sources"], talk["hits"]) == (["transcript", "run"], []), "neither the chats nor the local runs"
    refused = agent_call(lab, spec["id"], "history_search", {"query": "dial", "sources": ["chat", "run"]})
    assert refused["isError"] is True and "searches no group chat" in refused["content"][0]["text"]
    runs_listed = agent_call(lab, spec["id"], "history_sessions", {"kind": "run"})["structuredContent"]
    assert mine not in [item["run_id"] for item in runs_listed["items"]]
    events = agent_call(lab, spec["id"], "run_events", {"run_id": mine})
    assert events["isError"] is True and f"has no run {mine}" in events["content"][0]["text"]
    assert search(lab, q="dial")["total"] == 2, "the member reads as before"


def test_chat_hits_and_the_clusters_read_through_the_callers_sink(lab):
    registered = lab.client.put(f"/v1/projects/{PROJECT}", json=registration(SINKS), headers=lab.headers["admin"])
    assert registered.status_code == 200, registered.text
    night_talk(lab)
    assert search(lab, q="dial timeout", source="chat")["total"] == 1  # through the project's hub sink
    narrow = {**lab.headers["owner"], "X-Evo-Sink": "codex@openai"}  # clears public, below the default label
    path = f"/v1/projects/{PROJECT}/curator/history"
    through = lab.client.get(f"{path}/search", params={"q": "dial timeout", "source": "chat"}, headers=narrow)
    assert through.status_code == 200, through.text
    assert through.json()["total"] == 0, "a chat's message reads through the sink as a transcript does"
    assert lab.client.get(f"{path}/clusters", headers=narrow).status_code == 404
    refused = member_call(lab, "history_clusters", {}, sink="codex@openai")
    assert refused["isError"] is True and "you may read through this sink" in refused["content"][0]["text"]
    assert not member_call(lab, "history_clusters", {}).get("isError")  # through claude-code@anthropic

    review = held_review(lab)  # the Reviewer's chat hits read through its sink as the owner's do
    query = {"query": "dial timeout", "sources": ["chat"]}
    assert agent_call(lab, review, "history_search", query)["structuredContent"]["total"] == 1
    assert agent_call(lab, review, "history_search", query, sink="codex@openai")["structuredContent"]["total"] == 0


# The command line


def test_the_command_line_reads_the_history_with_the_json_keys_of_the_contract(lab, tmp_path, monkeypatch, capsys):
    two_sessions(lab)
    assert analyzed(lab.client, later(lab.db)) == {"transcript": 2}
    with serving(create_app(lab.client.app.state.config)) as url:
        token = lab.headers["owner"]["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, OWNER, token)

        def run(*args: str):
            return cli(monkeypatch, capsys, home, "hub", "curator", "history", *args, "--project", PROJECT)

        found = as_json(run("search", "AssertionError", "--source", "transcript", "--json"))
        assert_json_keys("hub curator history search", found)
        assert found["total"] == 2 and found["sources"] == ["transcript"]
        out = ok(run("search", "rejected")).out
        assert f"transcript:{FIRST}:13 ({OWNER}, 2026-10-08 01:00, Bash failed):" in out
        assert "2 of 2 hits of 'rejected' in evo-agents" in out
        sessions = as_json(run("sessions", "--kind", "session", "--json"))
        assert_json_keys("hub curator history sessions", sessions)
        assert sessions["total"] == 2
        out = ok(run("sessions", "--login", OWNER)).out
        assert f"session:{FIRST}" in out and "2 of 2 sessions and runs of evo-agents" in out
        shown = as_json(run("lines", FIRST, "5", "--before", "1", "--after", "0", "--json"))
        assert_json_keys("hub curator history lines", shown)
        assert [entry["line"] for entry in shown["entries"]] == [4, 5]
        out = ok(run("lines", FIRST, "5", "--before", "0", "--after", "0")).out
        assert f"> L5 Bash failed: {FAILED_TEST}" in out


# Schema 0057


def test_schema_0057_goes_down_to_0056_and_up_again(lab):
    two_sessions(lab)
    move_to(lab.db, "0056", down=True)
    names = {row[0] for row in sql(lab.db, select(INFO.c.table_name).where(INFO.c.table_schema == "public"))}
    assert not names & set(pg.SEARCH_TABLES)
    move_to(lab.db, "0057")
    assert rows_of(lab.db) == []
    assert indexed(lab.client) == {"rows": 27, "transcripts": 2}, "the index is made again from the parts"
