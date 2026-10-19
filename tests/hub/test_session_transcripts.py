"""Session transcripts on the hub (``evo_agents.hub.server.transcripts``) and the commands that send them
(``evo_agents.hub.transcript_sync``, ``evo-agents hub sessions``).

Step 2 of plan history-driven-curator. Criterion a1: every transcript of a session of a hub project, subagents'
included, whatever its number of messages, goes to the hub in parts as the session goes on, a part sent twice is
stored once, a client that went down loses nothing, and ``hub sessions backfill`` sends what is still on the machine
and sends nothing the second time. Criterion a2: the hub keeps a transcript EVO_HUB_TRANSCRIPT_DAYS (365) days after
its latest part, an admin of the project deletes one session's or all of them, and the project's usage reads through
the API and the command line. Around them: the write rule, the read rule with the label and the sink, the owner of a
transcript, the blobs in the store, the job, and schema 0049."""

import gzip
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import func, select, update

from evo_agents.hub import client as hub_client
from evo_agents.hub import jobs, tables
from evo_agents.hub import transcripts as local
from evo_agents.hub.blobs import blob_key
from evo_agents.hub.config import ConfigError, load_config
from evo_agents.hub.memory import slug
from evo_agents.hub.server.app import create_app
from evo_agents.hub.transcript_prune import prune_transcripts
from evo_agents.hub.worker import queue
from tests.hub.live import bearer, sql
from tests.hub.s3 import put_presigned
from tests.hub.test_digest import SESSION, said, session_lines
from tests.hub.test_hooks import live_project, machine
from tests.hub.test_migrate import move_to
from tests.hub.test_plans import registration
from tests.hub.test_redact import ANTHROPIC_KEY
from tests.hub.test_runs import OWNER, PROJECT, members
from tests.hub.test_session_digests import PUBLIC_READER, SINKS

OTHER_SESSION = "0199b1c2-7d3e-7a10-9c41-5e2f3a4b5c6e"
CODEX_SESSION = "01a12c1b-f58c-7672-acfc-cb7f35d3469a"


@pytest.fixture
def hub(hub_db, tmp_path, github, s3):
    """test_runs' members of project evo-agents, a reader up to public, the hub admin as an admin of the project, and
    a blob store."""
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        registered = client.put(f"/v1/projects/{PROJECT}", json=registration(SINKS), headers=headers["admin"])
        assert registered.status_code == 200, registered.text
        headers["public"] = bearer(live.sign_in(client, github, PUBLIC_READER, 690)["token"])
        for login, grant in ((PUBLIC_READER, ("reader", "public")), (live.ADMIN, ("admin", "secret"))):
            body = {"role": grant[0], "max_level": grant[1]}
            granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=body, headers=headers["admin"])
            assert granted.status_code == 200, granted.text
        yield SimpleNamespace(client=client, headers=headers, db=hub_db, tmp_path=tmp_path, s3=s3)


def transcript(tmp_path: Path, lines: list | None = None, name: str = "t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(line) + "\n" for line in (lines or session_lines())), encoding="utf-8")
    return path


def found(path: Path, session: str = SESSION, file: str = local.MAIN) -> local.Found:
    stat = path.stat()
    return local.Found(path, session, file, local.CLAUDE, slug("/work/app"), "/work/app", stat.st_mtime, stat.st_size)


def parts_of(hub, path: Path, start: int = 0, line: int = 1, limit: int = local.PART_BYTES, **where) -> list:
    """The parts of the transcript at ``path`` from byte ``start``, each in a file of its own."""
    item, parts = found(path, **where), []
    while (part := local.read_part(item, start, line, [], hub.tmp_path / "parts", limit=limit)) is not None:
        parts.append(part)
        start, line = part.end, part.first_line + part.lines
    return parts


def send(hub, who: str, parts: list, project: str = PROJECT, upload: bool = True):
    """Upload the blobs of ``parts`` the project lacks, then send the parts; the parts request's response."""
    ids = []
    if upload:
        items = {part.sha256: {"sha256": part.sha256, "size": part.size, "kind": local.KIND} for part in parts}
        asked = hub.client.post(
            "/v1/blobs/uploads", json={"project": project, "items": list(items.values())}, headers=hub.headers[who]
        )
        assert asked.status_code == 200, asked.text
        by_hash = {part.sha256: part for part in parts}
        for ticket in asked.json()["uploads"]:
            assert put_presigned(ticket["url"], by_hash[ticket["sha256"]].path.read_bytes()) == 200
            ids.append(ticket["upload_id"])
    body = {"upload_ids": ids, "parts": [part.wire("laptop") for part in parts]}
    return hub.client.post(f"/v1/projects/{project}/transcripts/parts", json=body, headers=hub.headers[who])


def statuses(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [part["status"] for part in response.json()["parts"]]


def records(db, query) -> list:
    """The rows of ``query`` with their columns by name."""
    with live.engine(db).begin() as conn:
        return [SimpleNamespace(**row._mapping) for row in conn.execute(query)]


def index(db) -> dict:
    t = tables.session_transcripts
    rows = records(
        db, select(t.c.session_id, t.c.file, t.c.source_bytes, t.c.lines, t.c.parts, t.c.sha256, t.c.deleted_at)
    )
    return {(row.session_id, row.file): row for row in rows}


def get(hub, who: str, path: str, sink: str | None = None, **params):
    headers = {**hub.headers[who], **({"X-Evo-Sink": sink} if sink else {})}
    return hub.client.get(f"/v1/projects/{PROJECT}/transcripts{path}", params=params, headers=headers)


# Sending parts


def test_a_writer_sends_a_transcript_in_parts_and_the_hub_keeps_them_in_order_in_the_blob_store(hub):
    path = transcript(hub.tmp_path)
    parts = parts_of(hub, path, limit=400)
    assert len(parts) >= 3
    assert statuses(send(hub, "owner", parts[:2])) == ["stored", "stored"]
    assert statuses(send(hub, "owner", parts[2:])) == ["stored"] * len(parts[2:])
    row = index(hub.db)[(SESSION, local.MAIN)]
    assert (row.source_bytes, row.lines, row.parts) == (path.stat().st_size, len(session_lines()), len(parts))
    hashes = None
    for part in parts:
        hashes = local.chain(hashes, part.sha256)
    assert row.sha256 == hashes
    held = b"".join(gzip.decompress(hub.s3.get(blob_key(part.sha256))) for part in parts)
    assert held == path.read_bytes(), "nothing to redact: the hub holds the file's very bytes"
    b = tables.blobs
    kinds = sql(hub.db, select(b.c.kind, func.count()).group_by(b.c.kind))
    assert dict(kinds) == {local.KIND: len(parts)}

    shown = get(hub, "owner", f"/{SESSION}")
    assert shown.status_code == 200, shown.text
    (main,) = shown.json()["transcripts"]
    assert (main["login"], main["machine"], main["runtime"], main["cwd"]) == (
        OWNER,
        "laptop",
        "claude-code",
        "/work/app",
    )
    assert main["label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}
    assert [p["seq"] for p in main["part_list"]] == list(range(1, len(parts) + 1))
    assert main["part_list"][1]["source_start"] == main["part_list"][0]["source_end"]
    assert main["started_at"].startswith("2026-10-08T01:00:00") and main["ended_at"].startswith("2026-10-08T01:00:14")


def test_a_part_sent_twice_is_stored_once_and_one_out_of_place_says_where_the_hub_s_copy_ends(hub):
    path = transcript(hub.tmp_path)
    first, *rest = parts_of(hub, path, limit=400)
    assert statuses(send(hub, "owner", rest[:1])) == ["conflict"], "no first part yet: start from byte 0"
    assert statuses(send(hub, "owner", [first], upload=False)) == ["missing"], "its blob was never uploaded"
    assert index(hub.db) == {}
    assert statuses(send(hub, "owner", [first])) == ["stored"]
    again = send(hub, "owner", [first])
    assert statuses(again) == ["held"] and again.json()["stored"] == 0
    skipped = send(hub, "owner", rest[1:2])
    assert statuses(skipped) == ["conflict"]
    assert (
        skipped.json()["parts"][0]["source_bytes"] == first.end and skipped.json()["parts"][0]["lines"] == first.lines
    )
    row = index(hub.db)[(SESSION, local.MAIN)]
    assert (row.parts, row.source_bytes) == (1, first.end)
    assert statuses(send(hub, "owner", rest)) == ["stored"] * len(rest)


def test_only_the_member_who_sent_a_transcript_first_adds_to_it_and_the_write_rule_holds(hub):
    path = transcript(hub.tmp_path)
    first, second, *_ = parts_of(hub, path, limit=400)
    assert statuses(send(hub, "owner", [first])) == ["stored"]
    assert statuses(send(hub, "other", [second])) == ["refused"]
    assert send(hub, "reader", [second], upload=False).status_code == 403
    assert (
        hub.client.post(
            f"/v1/projects/{PROJECT}/transcripts/held", json={"session_ids": [SESSION]}, headers=hub.headers["reader"]
        ).status_code
        == 403
    )
    from tests.hub.test_runs import add_worker

    worker = add_worker(hub.client, hub.headers["owner"], "mac-mini")["headers"]
    refused = hub.client.post(
        f"/v1/projects/{PROJECT}/transcripts/parts", json={"parts": [second.wire("laptop")]}, headers=worker
    )
    assert refused.status_code == 403
    tooling = second.wire("laptop") | {"file": "../escape"}
    bad = hub.client.post(
        f"/v1/projects/{PROJECT}/transcripts/parts", json={"parts": [tooling]}, headers=hub.headers["owner"]
    )
    assert bad.status_code == 422
    held = hub.client.post(
        f"/v1/projects/{PROJECT}/transcripts/held", json={"session_ids": [SESSION]}, headers=hub.headers["owner"]
    )
    assert held.json() == {
        "transcripts": [
            {
                "session_id": SESSION,
                "file": local.MAIN,
                "runtime": "claude-code",
                "source_bytes": first.end,
                "lines": first.lines,
                "parts": 1,
                "deleted": False,
            }
        ]
    }
    other = hub.client.post(
        f"/v1/projects/{PROJECT}/transcripts/held", json={"session_ids": [SESSION]}, headers=hub.headers["other"]
    )
    assert other.json() == {"transcripts": []}, "held answers the caller's own"


def test_reading_transcripts_needs_a_grant_and_a_label_the_grant_and_the_sink_clear(hub):
    path = transcript(hub.tmp_path)
    assert statuses(send(hub, "owner", parts_of(hub, path))) == ["stored"]
    sub = transcript(hub.tmp_path, [said("a subagent", 1)], name="sub.jsonl")
    assert statuses(send(hub, "owner", parts_of(hub, sub, file="subagents/agent-1.jsonl"))) == ["stored"]
    listed = get(hub, "reader", "")
    assert listed.status_code == 200 and listed.json()["total"] == 2
    assert {t["file"] for t in listed.json()["transcripts"]} == {local.MAIN, "subagents/agent-1.jsonl"}
    shown = get(hub, "reader", f"/{SESSION}")
    assert [t["file"] for t in shown.json()["transcripts"]] == [local.MAIN, "subagents/agent-1.jsonl"]
    assert get(hub, "public", "").json()["total"] == 0, "a grant up to public reads no internal transcript"
    assert get(hub, "public", f"/{SESSION}").status_code == 404
    assert get(hub, "reader", "", sink="codex@openai").json()["total"] == 0, "nor through a sink up to public"
    assert get(hub, "stranger", "").status_code == 404
    assert get(hub, "reader", "", login=OWNER, session_id=SESSION, runtime="claude-code").json()["total"] == 2
    assert get(hub, "reader", "", machine="desktop").json()["total"] == 0
    assert get(hub, "reader", "", limit=1, offset=1).json()["transcripts"][0]["file"] in {
        local.MAIN,
        "subagents/agent-1.jsonl",
    }


def test_the_usage_of_a_project_counts_its_transcripts_per_member_with_the_retention(hub):
    path = transcript(hub.tmp_path)
    mine = parts_of(hub, path, limit=400)
    assert set(statuses(send(hub, "owner", mine))) == {"stored"}
    theirs = parts_of(hub, transcript(hub.tmp_path, [said("other", 2)], name="o.jsonl"), session=OTHER_SESSION)
    assert statuses(send(hub, "other", theirs)) == ["stored"]
    usage = get(hub, "reader", "/usage")
    assert usage.status_code == 200, usage.text
    found_ = usage.json()
    assert (found_["project"], found_["sessions"], found_["transcripts"], found_["deleted"]) == (PROJECT, 2, 2, 0)
    assert found_["parts"] == len(mine) + 1
    assert found_["bytes"] == sum(p.size for p in mine + theirs) == found_["blob_bytes"]
    assert found_["source_bytes"] == path.stat().st_size + theirs[0].end
    assert found_["retention_days"] == 365
    assert [m["login"] for m in found_["members"]] == [OWNER, "someone-else"]
    assert get(hub, "stranger", "/usage").status_code == 404


# Deleting and the retention


def test_an_admin_deletes_one_session_s_transcripts_then_all_and_their_blobs_leave_the_store(hub):
    mine = parts_of(hub, transcript(hub.tmp_path), limit=400)
    assert set(statuses(send(hub, "owner", mine))) == {"stored"}
    other = parts_of(hub, transcript(hub.tmp_path, [said("o", 1)], name="o.jsonl"), session=OTHER_SESSION)
    assert statuses(send(hub, "owner", other)) == ["stored"]
    assert (
        hub.client.delete(f"/v1/projects/{PROJECT}/transcripts/{SESSION}", headers=hub.headers["owner"]).status_code
        == 403
    )
    gone = hub.client.delete(f"/v1/projects/{PROJECT}/transcripts/{SESSION}", headers=hub.headers["admin"])
    assert gone.status_code == 200, gone.text
    assert gone.json() == {
        "project": PROJECT,
        "session_id": SESSION,
        "transcripts": 1,
        "parts": len(mine),
        "bytes": sum(part.size for part in mine),
    }
    row = index(hub.db)[(SESSION, local.MAIN)]
    assert row.deleted_at is not None and row.parts == 0 and row.source_bytes == mine[-1].end
    assert all(hub.s3.get(blob_key(part.sha256)) is None for part in mine)
    assert hub.s3.get(blob_key(other[0].sha256)) is not None
    assert statuses(send(hub, "owner", mine[:1], upload=False)) == ["deleted"], "the hub takes no more of it"
    shown = get(hub, "reader", f"/{SESSION}").json()["transcripts"][0]
    assert shown["deleted_at"] is not None and shown["part_list"] == []
    assert (
        hub.client.delete(
            f"/v1/projects/{PROJECT}/transcripts/{CODEX_SESSION}", headers=hub.headers["admin"]
        ).status_code
        == 404
    )

    everything = hub.client.delete(f"/v1/projects/{PROJECT}/transcripts", headers=hub.headers["admin"])
    assert everything.json()["session_id"] is None and everything.json()["transcripts"] == 1
    assert hub.s3.get(blob_key(other[0].sha256)) is None
    usage = get(hub, "reader", "/usage").json()
    assert (usage["transcripts"], usage["deleted"], usage["bytes"], usage["blob_bytes"]) == (0, 2, 0, 0)
    a = tables.audit
    trail = sql(hub.db, select(a.c.action, a.c.target).where(a.c.action == "transcript.delete").order_by(a.c.id))
    assert trail == [
        ("transcript.delete", f"{PROJECT} session={SESSION} transcripts=1"),
        ("transcript.delete", f"{PROJECT} all transcripts=1"),
    ]


def test_transcripts_without_a_part_for_the_retention_lose_their_content_and_unnamed_blobs_go(hub):
    old = parts_of(hub, transcript(hub.tmp_path))
    fresh = parts_of(hub, transcript(hub.tmp_path, [said("new", 1)], name="n.jsonl"), session=OTHER_SESSION)
    assert statuses(send(hub, "owner", old)) == ["stored"] and statuses(send(hub, "owner", fresh)) == ["stored"]
    lost = parts_of(hub, transcript(hub.tmp_path, [said("never recorded", 1)], name="l.jsonl"), session=CODEX_SESSION)
    asked = hub.client.post(
        "/v1/blobs/uploads",
        json={"project": PROJECT, "items": [{"sha256": lost[0].sha256, "size": lost[0].size, "kind": local.KIND}]},
        headers=hub.headers["owner"],
    ).json()["uploads"][0]
    put_presigned(asked["url"], lost[0].path.read_bytes())
    committed = hub.client.post(
        "/v1/blobs/commit", json={"project": PROJECT, "upload_ids": [asked["upload_id"]]}, headers=hub.headers["owner"]
    )
    assert committed.status_code == 200
    t, b = tables.session_transcripts, tables.blobs
    long_ago = func.now() - timedelta(days=366)
    sql(hub.db, update(t).values(created_at=long_ago, updated_at=long_ago).where(t.c.session_id == SESSION))
    sql(hub.db, update(b).values(verified_at=func.now() - timedelta(days=2)).where(b.c.sha256 == lost[0].sha256))

    state = hub.client.app.state
    context = SimpleNamespace(
        additional_context={
            "hub": SimpleNamespace(engine=state.engine, blobs=state.blobs, config=SimpleNamespace(transcript_days=365))
        }
    )
    result = hub.client.portal.call(queue.tasks[jobs.PRUNE_TRANSCRIPTS].func, context)
    assert result == {"transcripts": 1, "parts": 1, "bytes": old[0].size, "orphans": 1, "days": 365}
    rows = index(hub.db)
    assert rows[(SESSION, local.MAIN)].deleted_at is not None and rows[(OTHER_SESSION, local.MAIN)].deleted_at is None
    assert hub.s3.get(blob_key(old[0].sha256)) is None and hub.s3.get(blob_key(lost[0].sha256)) is None
    assert hub.s3.get(blob_key(fresh[0].sha256)) is not None
    again = hub.client.portal.call(prune_transcripts, state.engine, state.blobs, 365)
    assert again == {"transcripts": 0, "parts": 0, "bytes": 0, "orphans": 0, "days": 365}


def test_the_transcript_pruning_runs_daily_once_and_its_days_are_configured():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.PRUNE_TRANSCRIPTS].cron == "53 4 * * *"
    assert queue.tasks[jobs.PRUNE_TRANSCRIPTS].queueing_lock == jobs.PRUNE_TRANSCRIPTS
    base = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    assert load_config(base).transcript_days == 365
    assert load_config({**base, "EVO_HUB_TRANSCRIPT_DAYS": "730"}).transcript_days == 730
    for bad in ("29", "3651", "a year"):
        with pytest.raises(ConfigError, match="EVO_HUB_TRANSCRIPT_DAYS"):
            load_config({**base, "EVO_HUB_TRANSCRIPT_DAYS": bad})


def test_the_tables_of_0049_check_their_rows(hub):
    assert statuses(send(hub, "owner", parts_of(hub, transcript(hub.tmp_path)))) == ["stored"]
    t, p = tables.session_transcripts, tables.transcript_parts
    bad_rows = (
        (t, {"file": "../x"}),
        (t, {"file": "subagents"}),
        (t, {"runtime": "gemini"}),
        (t, {"machine": "a b"}),
        (t, {"lines": -1}),
        (t, {"sha256": "F" * 64}),
        (t, {"label": "internal"}),
        (t, {"updated_at": func.now() - timedelta(days=1)}),
        (p, {"source_end": 0}),
        (p, {"lines": 0}),
        (p, {"seq": 0}),
        (p, {"size": 0}),
    )
    for table, values in bad_rows:
        with pytest.raises(errors.CheckViolation):
            sql(hub.db, update(table).values(**values))


def test_migration_0049_goes_down_to_0048_and_up_again(hub):
    assert statuses(send(hub, "owner", parts_of(hub, transcript(hub.tmp_path)))) == ["stored"]
    move_to(hub.db, "0048", down=True)
    present = select(func.to_regclass("public.session_transcripts"), func.to_regclass("public.transcript_parts"))
    assert sql(hub.db, present) == [(None, None)]
    assert sql(hub.db, select(func.count()).select_from(tables.blobs).where(tables.blobs.c.kind == local.KIND)) == [
        (1,)
    ]
    move_to(hub.db, "0049")
    assert index(hub.db) == {}


# The machine's side, against a hub of its own


def claude_lines(token: str) -> list[dict]:
    return [*session_lines(turn=f"Use {ANTHROPIC_KEY}; my hub token is {token}."), said("one more", 15)]


def codex_rollout(cwd: str) -> list[dict]:
    meta = {"timestamp": "2026-10-08T01:00:00Z", "type": "session_meta", "payload": {"session_id": CODEX_SESSION}}
    meta["payload"] |= {"id": CODEX_SESSION, "cwd": cwd}
    return [meta, {"timestamp": "2026-10-08T01:00:01Z", "type": "turn_context", "payload": {"model": "gpt-6"}}]


def write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def evo(home: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return pg.cli(["hub", *args], pg.clean_env(HOME=str(home)), timeout=timeout)


def laptop(hub_db, tmp_path, url: str) -> SimpleNamespace:
    """A machine of alice, writer on demo, with a Claude Code session of the harness (and a subagent), a Codex one,
    and a session of a directory of no project."""
    admin = hub_client.Hub(url, live.insert_token(hub_db, live.ADMIN))
    admin.call("PUT", "/v1/projects/demo", live_project())
    admin.call("PUT", "/v1/admin/projects/demo/grants/alice", {"role": "writer", "max_level": "internal"})
    token = live.insert_token(hub_db, "alice")
    home, root = machine(tmp_path, "laptop", url, "alice", token)
    projects = home / ".claude" / "projects"
    main = write(projects / slug(root) / f"{SESSION}.jsonl", claude_lines(token))
    sub = write(projects / slug(root) / SESSION / "subagents" / "agent-a1.jsonl", [said("subagent work", 3)])
    codex = write(home / ".codex" / "sessions" / "2026" / "10" / "08" / "rollout-a.jsonl", codex_rollout(str(root)))
    write(projects / slug(home / "scratch") / f"{OTHER_SESSION}.jsonl", session_lines())
    return SimpleNamespace(home=home, root=root, token=token, main=main, sub=sub, codex=codex, admin=admin)


def report(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def held_on_hub(db) -> dict:
    t = tables.session_transcripts
    rows = records(db, select(t.c.session_id, t.c.file, t.c.source_bytes, t.c.lines, t.c.runtime))
    return {(row.session_id, row.file): row for row in rows}


def content(s3, db, session: str, file: str = local.MAIN) -> bytes:
    t, p = tables.session_transcripts, tables.transcript_parts
    hashes = sql(
        db,
        select(p.c.sha256)
        .join_from(p, t, t.c.id == p.c.transcript_id)
        .where(t.c.session_id == session, t.c.file == file)
        .order_by(p.c.seq),
    )
    return b"".join(gzip.decompress(s3.get(blob_key(sha))) for (sha,) in hashes)


def test_backfill_sends_every_transcript_of_the_machine_once_and_a_second_run_sends_nothing(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        m = laptop(hub_db, tmp_path, served.url)
        dry = report(evo(m.home, "sessions", "backfill", "--dry-run", "--json"))
        assert (dry["dry_run"], dry["sessions"], dry["transcripts"], dry["missing"]) == (True, 2, 3, 3)
        assert dry["missing_ended"] == 0 and dry["uploaded"]["parts"] == 0
        assert dry["skipped"] == {"of no hub project": 1} and held_on_hub(hub_db) == {}

        sent = report(evo(m.home, "sessions", "backfill", "--json"))
        assert (sent["missing"], sent["complete"], sent["uploaded"]["parts"], sent["uploaded"]["transcripts"]) == (
            0,
            3,
            3,
            3,
        )
        held = held_on_hub(hub_db)
        assert sorted(held) == sorted(
            [(SESSION, local.MAIN), (SESSION, "subagents/agent-a1.jsonl"), (CODEX_SESSION, local.MAIN)]
        )
        assert held[(SESSION, local.MAIN)].source_bytes == m.main.stat().st_size
        assert held[(SESSION, local.MAIN)].lines == len(m.main.read_text().splitlines())
        assert held[(CODEX_SESSION, local.MAIN)].runtime == "codex"
        stored = content(s3, hub_db, SESSION).decode()
        assert ANTHROPIC_KEY not in stored and m.token not in stored and "***" in stored
        assert len(stored.splitlines()) == len(m.main.read_text().splitlines())

        second = report(evo(m.home, "sessions", "backfill", "--json"))
        assert (second["uploaded"]["parts"], second["missing"], second["complete"]) == (0, 0, 3)
        (m.home / ".evo" / "hub" / "transcripts" / local.STATE_FILE).unlink()
        lost = report(evo(m.home, "sessions", "backfill", "--project", "demo", "--json"))
        assert (lost["uploaded"]["parts"], lost["complete"]) == (0, 3), "a machine that lost its state sends nothing"
        assert report(evo(m.home, "sessions", "backfill", "--project", "other", "--json"))["transcripts"] == 0
        later = report(evo(m.home, "sessions", "backfill", "--since", "2999-01-01", "--dry-run", "--json"))
        assert later["transcripts"] == 0
        bad = evo(m.home, "sessions", "backfill", "--since", "last week")
        assert bad.returncode == 2 and "--since" in bad.stderr

        with open(m.main, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(said("the session goes on", 20)) + "\n")
        grown = report(evo(m.home, "sessions", "sync", "--json"))
        assert grown["uploaded"]["parts"] == 1 and grown["missing"] == 0
        assert content(s3, hub_db, SESSION).decode().splitlines()[-1] == json.dumps(said("the session goes on", 20))
    assert ANTHROPIC_KEY not in served.log() and "Run the tests" not in served.log()


def test_a_sync_while_the_hub_is_down_loses_nothing_and_the_next_one_sends_it(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        m = laptop(hub_db, tmp_path, served.url)
        config = m.home / ".evo" / "hub" / "config.json"
        config.write_text(json.dumps({"url": f"http://127.0.0.1:{pg.free_port()}", "login": "alice"}))
        down = evo(m.home, "sessions", "sync", "--json")
        assert down.returncode == 1 and "did not answer" in down.stderr + down.stdout
        assert held_on_hub(hub_db) == {}
        config.write_text(json.dumps({"url": served.url, "login": "alice"}))
        up = report(evo(m.home, "sessions", "sync", "--json"))
        assert up["uploaded"]["transcripts"] == 3 and held_on_hub(hub_db).keys() == {
            (SESSION, local.MAIN),
            (SESSION, "subagents/agent-a1.jsonl"),
            (CODEX_SESSION, local.MAIN),
        }
        assert report(evo(m.home, "sessions", "sync", "--json"))["uploaded"]["parts"] == 0
        log = (m.home / ".evo" / "hub" / "transcripts" / local.LOG_FILE).read_text()
        assert len(log.splitlines()) == 3 and "did not answer" in log.splitlines()[0]


def test_stop_queues_the_transcript_and_a_background_sync_sends_it_without_holding_the_hook(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        m = laptop(hub_db, tmp_path, served.url)
        env = pg.clean_env(HOME=str(m.home), CLAUDE_PROJECT_DIR=str(m.root))
        payload = {"session_id": SESSION, "transcript_path": str(m.main), "cwd": str(m.root), "hook_event_name": "Stop"}
        started = time.monotonic()
        stopped = subprocess.run(
            [sys.executable, "-m", "evo_agents", "hub", "hook", "stop"],
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert (stopped.returncode, stopped.stdout, stopped.stderr) == (0, "", "")
        assert time.monotonic() - started < local.SETTLE, "the hook does not wait for the sync"
        deadline = time.monotonic() + local.SETTLE + 60
        while (SESSION, "subagents/agent-a1.jsonl") not in held_on_hub(hub_db) or (
            SESSION,
            local.MAIN,
        ) not in held_on_hub(hub_db):
            assert time.monotonic() < deadline, "the background sync did not send the session within a minute"
            time.sleep(0.5)
        while local.sync_running(m.home / ".evo" / "hub" / "transcripts"):
            assert time.monotonic() < deadline
            time.sleep(0.2)
        assert local.Queue(m.home / ".evo" / "hub" / "transcripts").entries() == {}
        assert held_on_hub(hub_db)[(SESSION, local.MAIN)].source_bytes == m.main.stat().st_size


def test_the_sessions_commands_list_show_count_and_delete_on_the_hub(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        m = laptop(hub_db, tmp_path, served.url)
        assert report(evo(m.home, "sessions", "backfill", "--json"))["uploaded"]["transcripts"] == 3
        listed = report(evo(m.home, "sessions", "list", "--project", "demo", "--json"))
        assert listed["total"] == 3 and {t["login"] for t in listed["transcripts"]} == {"alice"}
        text = evo(m.home, "sessions", "list", "--project", "demo", "--session", SESSION)
        assert text.returncode == 0 and "2 of 2 transcript(s) of demo" in text.stdout
        shown = report(evo(m.home, "sessions", "show", SESSION, "--project", "demo", "--json"))
        assert [t["file"] for t in shown["transcripts"]] == [local.MAIN, "subagents/agent-a1.jsonl"]
        usage = report(evo(m.home, "sessions", "usage", "--project", "demo", "--json"))
        assert (usage["sessions"], usage["transcripts"], usage["retention_days"]) == (2, 3, 365)
        assert "kept 365 days" in evo(m.home, "sessions", "usage", "--project", "demo").stdout

        refused = evo(m.home, "sessions", "delete", SESSION, "--project", "demo")
        assert refused.returncode == 1 and "admin role" in refused.stderr
        assert evo(m.home, "sessions", "delete", "--project", "demo").returncode == 2
        m.admin.call("PUT", "/v1/admin/projects/demo/grants/alice", {"role": "admin", "max_level": "internal"})
        deleted = report(evo(m.home, "sessions", "delete", SESSION, "--project", "demo", "--json"))
        assert (deleted["session_id"], deleted["transcripts"]) == (SESSION, 2)
        again = report(evo(m.home, "sessions", "backfill", "--json"))
        assert again["uploaded"]["parts"] == 0 and again["skipped"]["deleted on the hub"] == 2, "deleted stays deleted"
        everything = report(evo(m.home, "sessions", "delete", "--all", "--project", "demo", "--json"))
        assert everything["transcripts"] == 1
