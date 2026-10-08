"""Session digests on the hub (``evo_agents.hub.server.digests``) and the Stop hook that pushes them.

The checks step 2 of the curator-agent plan names for criterion a3: the Stop hook of the evo-hub plugin pushes the
digest of every Claude Code session of 6 messages or more to the project of the session's directory, with every string
that looks like a secret replaced before it leaves the machine; the digest carries the project's label and only a
member whose grant and sink clear it reads it; a job deletes the digests not pushed for 90 days; a session inside a
worker run pushes none; and a digest the hub did not take waits for a later Stop. Around them: the write rule, the
owner of a session, the limits of a digest, and schema 0013."""

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents.hub import client as hub_client
from evo_agents.hub import jobs, tables
from evo_agents.hub.digest import STATE_FILE, DigestState, build
from evo_agents.hub.memory import slug
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.digests import DIGEST_DAYS, prune_digests
from evo_agents.hub.worker import queue
from tests.hub.live import bearer, sql
from tests.hub.test_digest import SESSION, answered, result, said, session_lines, tool_use, write_transcript
from tests.hub.test_hooks import closed_port_url, live_project, machine
from tests.hub.test_migrate import move_to
from tests.hub.test_plans import registration
from tests.hub.test_redact import ANTHROPIC_KEY, GITHUB_APP, HUB_TOKEN, LEASE_ENV, PEM, PEM_BODY
from tests.hub.test_runs import OWNER, PROJECT, members

OTHER_SESSION = "0199b1c2-7d3e-7a10-9c41-5e2f3a4b5c6e"
PUBLIC_READER = "public-reader"
SINKS = [
    {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}},
    {"id": "codex@openai", "kind": "agent-session", "clearance": {"level": "public"}},
    {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
]


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """test_runs' members of project evo-agents, a reader up to public, and a sink codex@openai that clears public."""
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        headers = members(client, github)
        registered = client.put(f"/v1/projects/{PROJECT}", json=registration(SINKS), headers=headers["admin"])
        assert registered.status_code == 200, registered.text
        headers["public"] = bearer(live.sign_in(client, github, PUBLIC_READER, 690)["token"])
        grant = {"role": "reader", "max_level": "public"}
        granted = client.put(
            f"/v1/admin/projects/{PROJECT}/grants/{PUBLIC_READER}", json=grant, headers=headers["admin"]
        )
        assert granted.status_code == 200, granted.text
        yield SimpleNamespace(client=client, headers=headers, db=hub_db, tmp_path=tmp_path)


def a_digest(tmp_path: Path, lines: list[dict] | None = None) -> dict:
    return build(write_transcript(tmp_path / "transcript.jsonl", lines or session_lines()), "/work/app")


def put(hub, who: str, body: dict, session: str = SESSION, project: str = PROJECT):
    return hub.client.put(f"/v1/projects/{project}/digests/{session}", json=body, headers=hub.headers[who])


def listed(hub, who: str, sink: str | None = None, **params) -> dict:
    headers = {**hub.headers[who], **({"X-Evo-Sink": sink} if sink else {})}
    response = hub.client.get(f"/v1/projects/{PROJECT}/digests", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def shown(hub, who: str, session: str = SESSION, sink: str | None = None):
    headers = {**hub.headers[who], **({"X-Evo-Sink": sink} if sink else {})}
    return hub.client.get(f"/v1/projects/{PROJECT}/digests/{session}", headers=headers)


def count(db) -> int:
    return sql(db, select(func.count()).select_from(tables.session_digests))[0][0]


# Pushing and reading


def test_a_writer_pushes_a_session_digest_and_a_later_turn_replaces_it(hub):
    body = a_digest(hub.tmp_path)
    first = put(hub, "owner", body)
    assert first.status_code == 200, first.text
    summary = first.json()
    assert summary["session_id"] == SESSION and summary["login"] == OWNER and summary["messages"] == 13
    assert summary["label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}
    assert summary["created_at"] == summary["updated_at"]

    later = a_digest(hub.tmp_path, [*session_lines(), said("And push it.", 40), answered("msg_9", 41)])
    again = put(hub, "owner", later)
    assert again.status_code == 200 and again.json()["messages"] == 15
    assert again.json()["created_at"] == summary["created_at"] < again.json()["updated_at"]
    assert count(hub.db) == 1

    record = shown(hub, "reader")
    assert record.status_code == 200, record.text
    digest = record.json()["digest"]
    assert digest["user_turns"] == ["Run the tests and fix what fails.", "And push it."]
    assert digest["tools"][0] == {"gen_ai.tool.name": "Bash", "calls": 3, "errors": 2}
    assert digest["bash"][0] == {"program": "python -m pytest", "calls": 2, "errors": 1}
    assert digest["usage"]["cache_read_input_tokens"] == 4000 * 6


def test_reading_a_digest_needs_a_grant_and_a_label_the_grant_and_the_sink_clear(hub):
    assert put(hub, "owner", a_digest(hub.tmp_path)).status_code == 200
    for who in ("owner", "other", "reader"):
        assert listed(hub, who)["total"] == 1 and shown(hub, who).status_code == 200
    assert listed(hub, "public") == {"digests": [], "total": 0, "limit": 50, "offset": 0}  # its grant: public only
    assert shown(hub, "public").status_code == 404
    assert listed(hub, "reader", sink="codex@openai")["total"] == 0  # a sink that clears public only
    assert shown(hub, "reader", sink="codex@openai").status_code == 404
    assert listed(hub, "reader", sink="claude-code@anthropic")["total"] == 1
    assert shown(hub, "stranger").status_code == 404  # no grant: as for a project it cannot see
    assert hub.client.get(f"/v1/projects/{PROJECT}/digests", headers=hub.headers["stranger"]).status_code == 404
    assert shown(hub, "admin").status_code == 403  # a hub admin without a grant manages, never reads
    assert shown(hub, "reader", session=OTHER_SESSION).status_code == 404


def test_the_list_filters_by_time_and_member_and_pages(hub):
    assert put(hub, "owner", a_digest(hub.tmp_path)).status_code == 200
    assert put(hub, "other", a_digest(hub.tmp_path), session=OTHER_SESSION).status_code == 200
    every = listed(hub, "reader")
    assert [d["session_id"] for d in every["digests"]] == [OTHER_SESSION, SESSION], "the latest pushed first"
    assert [d["session_id"] for d in listed(hub, "reader", login=OWNER)["digests"]] == [SESSION]
    page = listed(hub, "reader", limit=1, offset=1)
    assert page["total"] == 2 and [d["session_id"] for d in page["digests"]] == [SESSION]
    d = tables.session_digests
    sql(hub.db, update(d).values(created_at=func.now() - timedelta(days=3), updated_at=func.now() - timedelta(days=2)))
    since = (sql(hub.db, select(func.now()))[0][0] - timedelta(days=1)).isoformat()
    assert listed(hub, "reader", since=since)["total"] == 0


def test_a_digest_push_follows_the_write_rule_and_the_session_stays_its_pusher_s(hub):
    body = a_digest(hub.tmp_path)
    refused = put(hub, "reader", body)
    assert refused.status_code == 403 and "writer role" in refused.json()["message"]
    assert put(hub, "stranger", body).status_code == 404
    assert put(hub, "owner", body).status_code == 200
    taken = put(hub, "other", body)
    assert taken.status_code == 403 and "another member's" in taken.json()["message"]
    assert sql(
        hub.db,
        select(tables.users.c.login).join_from(
            tables.session_digests, tables.users, tables.users.c.id == tables.session_digests.c.user_id
        ),
    ) == [(OWNER,)]

    without_hub_sink = registration([SINKS[0]])
    assert (
        hub.client.put(f"/v1/projects/{PROJECT}", json=without_hub_sink, headers=hub.headers["admin"]).status_code
        == 200
    )
    closed = put(hub, "owner", body)
    assert closed.status_code == 422 and "declares no sink of kind hub" in closed.json()["message"]


def test_a_digest_is_checked_against_its_limits_and_unknown_fields_are_dropped(hub):
    body = a_digest(hub.tmp_path)
    for broken in (
        {**body, "messages": 5},
        {**body, "user_turns": ["turn"] * 41},
        {**body, "errors": [{"gen_ai.tool.name": "Bash", "text": "x" * 401, "n": 1}]},
        {**body, "tools": [{"gen_ai.tool.name": "", "calls": 1, "errors": 0}]},
        {**body, "started_at": "2026-10-08T01:00:00"},  # without its time zone
        {key: value for key, value in body.items() if key != "cwd"},
    ):
        response = put(hub, "owner", broken)
        assert response.status_code == 422, response.text
    assert put(hub, "owner", body, session="not/a-session").status_code == 404
    assert put(hub, "owner", body, session="-starts-with-a-dash").status_code == 422
    huge = hub.client.put(
        f"/v1/projects/{PROJECT}/digests/{SESSION}",
        content=b"{" + b" " * (2 * 1024 * 1024) + b"}",
        headers={**hub.headers["owner"], "Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    assert count(hub.db) == 0
    assert put(hub, "owner", {**body, "transcript": "/home/alice/secret.jsonl"}).status_code == 200
    stored = sql(hub.db, select(tables.session_digests.c.body))[0][0]
    assert "transcript" not in stored and stored["messages"] == 13


def test_a_worker_token_cannot_push_a_digest(hub):
    from tests.hub.test_runs import add_worker

    worker = add_worker(hub.client, hub.headers["owner"], "mac-mini")
    body = a_digest(hub.tmp_path)
    refused = hub.client.put(f"/v1/projects/{PROJECT}/digests/{SESSION}", json=body, headers=worker["headers"])
    assert refused.status_code == 403
    assert count(hub.db) == 0


# Retention and schema 0013


def test_digests_not_pushed_for_90_days_are_pruned_by_the_daily_job(hub):
    assert put(hub, "owner", a_digest(hub.tmp_path)).status_code == 200
    assert put(hub, "owner", a_digest(hub.tmp_path), session=OTHER_SESSION).status_code == 200
    d = tables.session_digests
    old = func.now() - timedelta(days=DIGEST_DAYS + 1)
    sql(hub.db, update(d).values(created_at=old, updated_at=old).where(d.c.session_id == SESSION))
    state = hub.client.app.state
    context = SimpleNamespace(additional_context={"hub": SimpleNamespace(engine=state.engine)})
    assert hub.client.portal.call(queue.tasks[jobs.PRUNE_DIGESTS].func, context) == {"deleted": 1, "days": 90}
    assert sql(hub.db, select(d.c.session_id)) == [(OTHER_SESSION,)]
    assert hub.client.portal.call(prune_digests, state.engine) == {"deleted": 0, "days": 90}


def test_the_digest_pruning_runs_daily_and_is_queued_at_most_once():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.PRUNE_DIGESTS].cron == "23 4 * * *"
    assert queue.tasks[jobs.PRUNE_DIGESTS].queueing_lock == jobs.PRUNE_DIGESTS


def test_the_digest_tables_of_0013_check_their_rows(hub):
    assert put(hub, "owner", a_digest(hub.tmp_path)).status_code == 200
    d, s = tables.session_digests, tables.run_tool_stats
    for values in ({"messages": 5}, {"body": [1]}, {"label": "internal"}, {"updated_at": func.now() - timedelta(1)}):
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(hub.db, update(d).values(**values))
    from tests.hub.test_runs import dispatched

    run_id = dispatched(hub.client, hub.headers["owner"], [2])[0]["id"]
    good = {"run_id": run_id, "tool_name": "Bash", "calls": 2, "errors": 2, "duration_ms": 0}
    sql(hub.db, insert(s).values(**good))
    for bad in ({"errors": 3}, {"calls": 0, "errors": 0}, {"duration_ms": -1}, {"tool_name": ""}):
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(hub.db, insert(s).values(**{**good, "tool_name": "Read", **bad}))


def test_digest_migration_0013_goes_down_to_0012_and_up_again(hub):
    assert put(hub, "owner", a_digest(hub.tmp_path)).status_code == 200
    move_to(hub.db, "0012", down=True)
    present = select(func.to_regclass("public.session_digests"), func.to_regclass("public.run_tool_stats"))
    assert sql(hub.db, present) == [(None, None)]
    move_to(hub.db, "0013")
    assert count(hub.db) == 0


# The Stop hook against hub serve


def stop_hook(home: Path, session_dir: Path, session: str, transcript: Path, **variables: str):
    """``evo-agents hub hook stop`` as Claude Code runs it at the end of a turn of ``session``."""
    env = pg.clean_env(HOME=str(home), CLAUDE_PROJECT_DIR=str(session_dir), **variables)
    payload = {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": str(session_dir),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    command = [sys.executable, "-m", "evo_agents", "hub", "hook", "stop"]
    return subprocess.run(command, env=env, input=json.dumps(payload), capture_output=True, text=True, timeout=60)


def secret_lines(token: str) -> list[dict]:
    """A session whose person, commands and errors hold a secret of each kind, the machine's own hub token among
    them."""
    lines = session_lines(turn=f"Use {ANTHROPIC_KEY} for the API, and my hub token is {token}.")
    lines += [
        answered("msg_6", 20, tool_use("toolu_6", "Bash", command=f"export GITHUB_TOKEN={GITHUB_APP} && gh pr list")),
        result("toolu_6", 21, f"HTTP 401 for token {HUB_TOKEN}", error=True),
        answered("msg_7", 22, tool_use("toolu_7", "Bash", command=f"export CLAUDE_CODE_OAUTH_TOKEN='{LEASE_ENV}'")),
        result("toolu_7", 23, f"cat: key.pem\n{PEM}", error=True),
    ]
    return lines


def transcript_of(home: Path, session_dir: Path, session: str, lines: list[dict]) -> Path:
    return write_transcript(home / ".claude" / "projects" / slug(session_dir) / f"{session}.jsonl", lines)


def stored(db) -> dict:
    d = tables.session_digests
    return {row[0]: row for row in sql(db, select(d.c.session_id, d.c.body, d.c.updated_at, d.c.messages))}


def test_stop_pushes_the_redacted_digest_of_a_session_to_the_project_of_its_directory(hub_db, tmp_path):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as served:
        admin = hub_client.Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", "/v1/projects/demo", live_project())
        admin.call("PUT", "/v1/admin/projects/demo/grants/alice", {"role": "writer", "max_level": "internal"})
        token = live.insert_token(hub_db, "alice")
        home, root = machine(tmp_path, "laptop", served.url, "alice", token)
        transcript = transcript_of(home, root, SESSION, secret_lines(token))

        pushed = stop_hook(home, root, SESSION, transcript)
        assert (pushed.returncode, pushed.stdout, pushed.stderr) == (0, "", "")
        held = stored(hub_db)
        assert list(held) == [SESSION] and held[SESSION][3] == 17
        text = json.dumps(held[SESSION][1])
        for secret in (ANTHROPIC_KEY, token, GITHUB_APP, HUB_TOKEN, LEASE_ENV, PEM_BODY):
            assert secret not in text
        assert "***" in text and held[SESSION][1]["cwd"] == str(root)
        assert held[SESSION][1]["tools"][0] == {"gen_ai.tool.name": "Bash", "calls": 5, "errors": 4}
        state = DigestState(home / ".evo" / "hub", served.url).get(SESSION)
        assert (state["project"], state["pending"]) == ("demo", False)
        alice = hub_client.Hub(served.url, token)
        shown_ = alice.call("GET", f"/v1/projects/demo/digests/{SESSION}")
        assert shown_["login"] == "alice" and shown_["digest"]["messages"] == 17

        # The same transcript again: nothing new, nothing sent; a later turn replaces the digest.
        before = held[SESSION][2]
        assert stop_hook(home, root, SESSION, transcript).returncode == 0
        assert stored(hub_db)[SESSION][2] == before
        write_transcript(transcript, [*secret_lines(token), said("Thanks.", 50)])
        assert stop_hook(home, root, SESSION, transcript).stderr == ""
        assert stored(hub_db)[SESSION][3] == 18

        # A session of a worker run sends none; a session in a directory of no project neither.
        run_session = transcript_of(home, root, OTHER_SESSION, session_lines())
        worker = stop_hook(home, root, OTHER_SESSION, run_session, EVO_RUN_ID="7")
        assert (worker.returncode, worker.stdout, worker.stderr) == (0, "", "")
        elsewhere = home / "scratch"
        elsewhere.mkdir()
        personal = transcript_of(home, elsewhere, OTHER_SESSION, session_lines())
        assert stop_hook(home, elsewhere, OTHER_SESSION, personal).stderr == ""
        assert list(stored(hub_db)) == [SESSION]
        assert DigestState(home / ".evo" / "hub", served.url).get(OTHER_SESSION)["project"] is None
    log = served.log()
    for secret in (ANTHROPIC_KEY, GITHUB_APP, LEASE_ENV, PEM_BODY, "Run the tests"):
        assert secret not in log


def test_a_digest_that_waited_while_the_hub_was_down_goes_with_a_later_stop(hub_db, tmp_path):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as served:
        admin = hub_client.Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", "/v1/projects/demo", live_project())
        admin.call("PUT", "/v1/admin/projects/demo/grants/alice", {"role": "writer", "max_level": "internal"})
        home, root = machine(tmp_path, "laptop", served.url, "alice", live.insert_token(hub_db, "alice"))
        config = home / ".evo" / "hub" / "config.json"
        down = closed_port_url()
        config.write_text(json.dumps({"url": down, "login": "alice"}), encoding="utf-8")
        first = transcript_of(home, root, SESSION, session_lines())
        failed = stop_hook(home, root, SESSION, first)
        assert (failed.returncode, failed.stdout) == (0, "") and "the hub did not answer" in failed.stderr
        assert stored(hub_db) == {}
        waiting = json.loads((home / ".evo" / "hub" / STATE_FILE).read_text(encoding="utf-8"))
        assert waiting["hubs"][down]["sessions"][SESSION]["pending"] is True

        # The hub moved back: the same machine's state for it is empty, so a stop of the session pushes it whole.
        config.write_text(json.dumps({"url": served.url, "login": "alice"}), encoding="utf-8")
        DigestState(home / ".evo" / "hub", served.url).put(
            SESSION, {"transcript": str(first), "cwd": str(root), "dir": str(root), "pending": True}
        )
        later = transcript_of(home, root, OTHER_SESSION, session_lines())
        pushed = stop_hook(home, root, OTHER_SESSION, later)
        assert (pushed.returncode, pushed.stdout, pushed.stderr) == (0, "", "")
        assert sorted(stored(hub_db)) == sorted([SESSION, OTHER_SESSION]), "the one that waited went too"
        state = DigestState(home / ".evo" / "hub", served.url)
        assert state.get(SESSION)["pending"] is False and state.waiting(OTHER_SESSION, 3) == []


def test_the_gen_ai_tool_name_key_is_the_one_the_hub_takes(hub):
    body = a_digest(hub.tmp_path)
    body["tools"] = [{"name": "Bash", "calls": 1, "errors": 0}]  # the field's own name works too
    assert put(hub, "owner", body).status_code == 200
    assert shown(hub, "owner").json()["digest"]["tools"] == [{"gen_ai.tool.name": "Bash", "calls": 1, "errors": 0}]
