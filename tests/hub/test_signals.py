"""Signals of a project on the hub (``evo_agents.hub.signals``), as criterion a3 of plan living-lean-canvas names them:
an admin creates an ingest key that writes signals and nothing else (its SHA-256 alone in the database, revocable); a
service sends batches of signals (a key, a number or one piece of feedback, a source, a time, a tenant, a level)
through POST /v1/signals/{project}; the keys are declared in the project's registry (unit, description, source); a
signal carries the project's label like everything else; members read them by key and range of time. Batches are
bounded in size and in rate, and the retention prunes old signals. The service, Northwind's knowledge base, is made
up.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents.hub import jobs, signals, tables
from evo_agents.hub.config import ConfigError, load_config
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import queue
from tests.hub import live
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_plans import registration
from tests.hub.test_runs import PROJECT

OTHER = "lumen"
ZERO_RESULTS = "kb.search.zero_result_rate"
TICKETS = "kb.support.feedback"
MEMBERS = {  # login -> (github id, role, max level)
    "owner": (701, "admin", "internal"),
    "alice": (702, "writer", "internal"),
    "eve": (705, "reader", "internal"),
    "outsider": (706, "reader", "public"),
}


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project with MEMBERS, a second project lumen owned by owner, and the registry's two keys; headers by login."""
    headers = {"admin": bearer(live.sign_in(client, github, ADMIN, 302)["token"])}
    for name in (PROJECT, OTHER):
        body = registration(OPEN_SINKS) | {"harness": {"name": name, "workspace": "~/ws", "path": f"{name}-harness"}}
        registered = client.put(f"/v1/projects/{name}", json=body, headers=headers["admin"])
        assert registered.status_code == 200, registered.text
    for login, (github_id, role, level) in MEMBERS.items():
        headers[login] = bearer(live.sign_in(client, github, login, github_id)["token"])
        grant = {"role": role, "max_level": level}
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
    owner_grant = {"role": "admin", "max_level": "internal"}
    granted = client.put(f"/v1/admin/projects/{OTHER}/grants/owner", json=owner_grant, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    key(client, headers["owner"], ZERO_RESULTS, "number", unit="ratio", source="northwind-kb")
    key(client, headers["owner"], TICKETS, "feedback", description="what readers wrote under an article")
    return headers


def key(client, headers, name: str, kind: str, expect: int = 201, project: str = PROJECT, **body) -> dict:
    answer = client.post(
        f"/v1/projects/{project}/signal-keys", json={"name": name, "kind": kind, **body}, headers=headers
    )
    assert answer.status_code == expect, answer.text
    return answer.json()


def ingest_key(client, headers, name: str = "northwind-kb", expect: int = 201, project: str = PROJECT, **body) -> dict:
    answer = client.post(f"/v1/projects/{project}/signal-ingest-keys", json={"name": name, **body}, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def push(client, headers, *items: dict, expect: int = 202, project: str = PROJECT) -> dict:
    answer = client.post(f"/v1/signals/{project}", json={"items": list(items)}, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def read(client, headers, expect: int = 200, **query) -> list:
    answer = client.get(f"/v1/projects/{PROJECT}/signals", params=query, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def at(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


# Ingest keys


def test_an_admin_creates_an_ingest_key_whose_token_is_shown_once_and_kept_as_its_hash(client, hub, hub_db):
    issued = ingest_key(client, hub["owner"], max_level="internal")
    token = issued["token"]
    assert token.startswith("evi_") and signals.INGEST_TOKEN.fullmatch(token)
    assert (issued["name"], issued["max_level"], issued["created_by"], issued["revoked_at"]) == (
        "northwind-kb",
        "internal",
        "owner",
        None,
    )
    ik = tables.signal_ingest_keys
    [(stored,)] = sql(hub_db, select(ik.c.token_hash))
    assert stored == hashlib.sha256(token.encode()).hexdigest() and token not in live.table_dump(hub_db)
    listed = client.get(f"/v1/projects/{PROJECT}/signal-ingest-keys", headers=hub["owner"]).json()
    assert [k["name"] for k in listed] == ["northwind-kb"] and "token" not in listed[0]
    assert token not in json.dumps(listed)
    for login in ("alice", "eve"):  # writers and readers neither see nor make ingest keys
        assert client.get(f"/v1/projects/{PROJECT}/signal-ingest-keys", headers=hub[login]).status_code == 403
        ingest_key(client, hub[login], name=f"by-{login}", expect=403)
    ingest_key(client, hub["owner"], expect=409)  # one live key of a name
    ingest_key(client, hub["owner"], name="above", max_level="top-secret", expect=422)
    audited = sql(
        hub_db, select(tables.audit.c.action, tables.audit.c.target).where(tables.audit.c.action.like("signal.%"))
    )
    assert ("signal.ingest-create", f"{PROJECT} ingest:{issued['id']} name=northwind-kb") in audited
    assert all(token not in target for _, target in audited)


def test_an_ingest_key_writes_signals_of_its_project_and_reaches_nothing_else(client, hub):
    token = ingest_key(client, hub["owner"])["token"]
    service = bearer(token)
    done = push(client, service, {"key": ZERO_RESULTS, "value": 0.12, "tenant": "northwind"})
    assert (done["accepted"], done["duplicates"], done["rejected"]) == (1, 0, [])
    refused = {
        ("GET", f"/v1/projects/{PROJECT}/signals"),  # not even the signals it wrote
        ("GET", f"/v1/signals/{PROJECT}"),
        ("GET", "/v1/projects"),
        ("GET", "/v1/auth/whoami"),
        ("POST", f"/v1/projects/{PROJECT}/signal-keys"),
        ("POST", f"/v1/projects/{PROJECT}/signal-ingest-keys"),
        ("GET", f"/v1/projects/{PROJECT}/plans"),
        ("POST", f"/v1/signals/{PROJECT}/more"),
    }
    for method, path in sorted(refused):
        answer = client.request(method, path, headers=service, json={})
        assert answer.status_code == 403, (method, path, answer.text)
        assert "only writes signals" in answer.json()["message"]
    worker = client.get("/v1/worker/runs", headers=service | {"X-Evo-Worker-Protocol": "1"})
    assert worker.status_code == 403
    other = client.post(f"/v1/signals/{OTHER}", json={"items": [{"key": ZERO_RESULTS, "value": 1}]}, headers=service)
    assert other.status_code == 403 and "another project" in other.json()["message"]
    unknown = client.post(
        f"/v1/signals/{PROJECT}", json={"items": [{"key": ZERO_RESULTS, "value": 1}]}, headers=bearer("evi_" + "A" * 43)
    )
    assert unknown.status_code == 401
    malformed = client.post(
        f"/v1/signals/{PROJECT}", json={"items": [{"key": ZERO_RESULTS, "value": 1}]}, headers=bearer("evi_short")
    )
    assert malformed.status_code == 401


def test_a_revoked_ingest_key_is_refused_at_its_next_batch(client, hub, hub_db):
    issued = ingest_key(client, hub["owner"])
    service = bearer(issued["token"])
    push(client, service, {"key": ZERO_RESULTS, "value": 0.3})
    revoked = client.post(f"/v1/projects/{PROJECT}/signal-ingest-keys/{issued['id']}/revoke", headers=hub["owner"])
    assert revoked.status_code == 200 and revoked.json()["revoked_by"] == "owner"
    again = client.post(f"/v1/projects/{PROJECT}/signal-ingest-keys/{issued['id']}/revoke", headers=hub["owner"])
    assert again.status_code == 200 and again.json()["revoked_at"] == revoked.json()["revoked_at"]
    answer = client.post(f"/v1/signals/{PROJECT}", json={"items": [{"key": ZERO_RESULTS, "value": 1}]}, headers=service)
    assert answer.status_code == 401 and "revoked or unknown" in answer.json()["message"]
    assert client.get(f"/v1/projects/{PROJECT}/signal-ingest-keys", headers=hub["owner"]).json() == []
    listed = client.get(f"/v1/projects/{PROJECT}/signal-ingest-keys?revoked=true", headers=hub["owner"]).json()
    assert [k["id"] for k in listed] == [issued["id"]]
    ingest_key(client, hub["owner"])  # the name is free again
    missing = client.post(f"/v1/projects/{PROJECT}/signal-ingest-keys/999999/revoke", headers=hub["owner"])
    assert missing.status_code == 404
    audited = sql(hub_db, select(tables.audit.c.action).where(tables.audit.c.action == "signal.ingest-revoke"))
    assert audited == [("signal.ingest-revoke",)]


# Batches


def test_each_item_of_a_batch_is_checked_alone_and_a_batch_sent_again_adds_nothing(client, hub, hub_db):
    service = bearer(ingest_key(client, hub["owner"], max_level="internal")["token"])
    items = [
        {"key": ZERO_RESULTS, "value": 0.12, "measured_at": at(30), "tenant": "northwind", "external_id": "z-1"},
        {"key": TICKETS, "text": "The article on returns is out of date", "source": "widget", "external_id": "t-1"},
        {"key": "kb.search.unknown", "value": 3, "external_id": "u-1"},
        {"key": ZERO_RESULTS, "text": "a number key takes no text"},
        {"key": TICKETS, "value": 2},
        {"key": TICKETS, "text": "x" * 4000 + "é" * 60},  # 4,060 characters, 4,120 bytes in UTF-8
        {"key": ZERO_RESULTS, "value": 0.2, "level": "customer"},  # above the key's max level
        {"key": ZERO_RESULTS, "value": 0.2, "level": "classified"},  # no such level
        {"key": ZERO_RESULTS, "value": 0.2, "measured_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        {"key": ZERO_RESULTS, "value": 0.2, "measured_at": (datetime.now(UTC) - timedelta(days=500)).isoformat()},
        {"key": ZERO_RESULTS, "value": 0.15, "level": "public", "synthetic": True, "external_id": "z-2"},
    ]
    done = push(client, service, *items)
    assert (done["accepted"], done["duplicates"]) == (3, 0)
    reasons = {item["index"]: item["reason"] for item in done["rejected"]}
    assert sorted(reasons) == [2, 3, 4, 5, 6, 7, 8, 9]
    assert "not in the registry" in reasons[2] and "takes a number" in reasons[3] and "takes feedback" in reasons[4]
    assert "4 KiB" in reasons[5] and "above internal, the highest ingest key northwind-kb writes" in reasons[6]
    assert "not a level of project" in reasons[7] and "ahead of the hub's clock" in reasons[8]
    assert "internal" not in reasons[7]  # an ingest key reads nothing of the project, its ladder included
    assert "older than the 400 days" in reasons[9]
    assert done["rejected"][0]["external_id"] == "u-1" and done["rejected"][0]["key"] == "kb.search.unknown"
    again = push(client, service, items[0], items[1], items[-1])
    assert (again["accepted"], again["duplicates"], again["rejected"]) == (0, 3, [])
    s = tables.signals
    stored = sql(hub_db, select(s.c.via, s.c.source, s.c.label).order_by(s.c.id))
    assert [source for _, source, _ in stored] == ["northwind-kb", "widget", "northwind-kb"]  # the key's by default
    assert {via for via, _, _ in stored} == {"ingest"}
    labels = [label for _, _, label in stored]
    assert labels[0] == {"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}
    assert labels[2]["level"] == "public"
    assert sql(hub_db, select(func.count(func.distinct(s.c.batch_id)))) == [(1,)]


def test_a_writer_pushes_with_a_machine_token_and_a_reader_may_not(client, hub, hub_db):
    done = push(client, hub["alice"], {"key": ZERO_RESULTS, "value": 0.4, "external_id": "manual-1"})
    assert done["accepted"] == 1
    again = push(client, hub["alice"], {"key": ZERO_RESULTS, "value": 0.4, "external_id": "manual-1"})
    assert (again["accepted"], again["duplicates"]) == (0, 1)
    refused = client.post(
        f"/v1/signals/{PROJECT}", json={"items": [{"key": ZERO_RESULTS, "value": 1}]}, headers=hub["eve"]
    )
    assert refused.status_code == 403
    s = tables.signals
    [(via, user_id, ingest_key_id)] = sql(hub_db, select(s.c.via, s.c.user_id, s.c.ingest_key_id))
    assert via == "member" and user_id is not None and ingest_key_id is None
    [shown] = read(client, hub["eve"])
    assert (shown["login"], shown["ingest_key"], shown["via"]) == ("alice", None, "member")


def test_a_batch_is_bounded_in_items_and_bytes(client, hub):
    service = bearer(ingest_key(client, hub["owner"])["token"])
    many = {"items": [{"key": ZERO_RESULTS, "value": index} for index in range(signals.MAX_ITEMS + 1)]}
    assert client.post(f"/v1/signals/{PROJECT}", json=many, headers=service).status_code == 422
    huge = json.dumps({"items": [{"key": ZERO_RESULTS, "value": 1, "source": "x" * signals.MAX_BATCH_BYTES}]})
    answer = client.post(f"/v1/signals/{PROJECT}", content=huge, headers=service | {"Content-Type": "application/json"})
    assert answer.status_code == 413 and "1024 KiB" in answer.json()["message"]
    assert client.post(f"/v1/signals/{PROJECT}", json={"items": []}, headers=service).status_code == 422


def test_an_ingest_key_past_its_rate_and_a_project_past_its_day_get_429_with_retry_after(client, hub, monkeypatch):
    monkeypatch.setattr(signals, "BATCHES_PER_MINUTE", 2)
    service = bearer(ingest_key(client, hub["owner"])["token"])
    for _ in range(2):
        push(client, service, {"key": ZERO_RESULTS, "value": 0.1})
    third = client.post(f"/v1/signals/{PROJECT}", json={"items": [{"key": ZERO_RESULTS, "value": 1}]}, headers=service)
    assert third.status_code == 429 and 1 <= int(third.headers["Retry-After"]) <= 60
    assert "more than 2 batches this minute" in third.json()["message"]
    monkeypatch.setattr(signals, "SIGNALS_PER_DAY", 4)
    push(client, hub["alice"], {"key": ZERO_RESULTS, "value": 0.2}, {"key": ZERO_RESULTS, "value": 0.3})
    over = client.post(
        f"/v1/signals/{PROJECT}",
        json={"items": [{"key": ZERO_RESULTS, "value": 0.4}, {"key": ZERO_RESULTS, "value": 0.5}]},
        headers=hub["alice"],
    )
    assert over.status_code == 429 and int(over.headers["Retry-After"]) > 3600
    assert "took 4 signals in the last 24 hours" in over.json()["message"]


def test_a_project_without_a_hub_sink_takes_no_signal(client, github, hub):
    sinks = [{"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}}]
    body = registration(sinks) | {"harness": {"name": OTHER, "workspace": "~/ws", "path": f"{OTHER}-harness"}}
    assert client.put(f"/v1/projects/{OTHER}", json=body, headers=hub["owner"]).status_code == 200
    key(client, hub["owner"], ZERO_RESULTS, "number", project=OTHER)
    refused = client.post(
        f"/v1/signals/{OTHER}", json={"items": [{"key": ZERO_RESULTS, "value": 1}]}, headers=hub["owner"]
    )
    assert refused.status_code == 422 and "no sink of kind hub" in refused.json()["message"]


# The registry of keys


def test_an_admin_keeps_the_registry_and_an_archived_key_takes_no_signal(client, hub, hub_db):
    listed = client.get(f"/v1/projects/{PROJECT}/signal-keys", headers=hub["eve"]).json()
    assert [(k["name"], k["kind"], k["unit"], k["source"]) for k in listed] == [
        (ZERO_RESULTS, "number", "ratio", "northwind-kb"),
        (TICKETS, "feedback", "", ""),
    ]
    key(client, hub["alice"], "kb.search.latency_ms", "number", expect=403)
    key(client, hub["owner"], ZERO_RESULTS, "number", expect=409)
    key(client, hub["owner"], "latency", "number", expect=422)  # one part only
    key(client, hub["owner"], "kb.search.latency_ms", "number", level="secret-ish", expect=422)
    made = key(client, hub["owner"], "kb.search.latency_ms", "number", unit="ms", level="public")
    assert (made["level"], made["created_by"], made["archived_at"]) == ("public", "owner", None)
    archived = client.post(f"/v1/projects/{PROJECT}/signal-keys/kb.search.latency_ms/archive", headers=hub["owner"])
    assert archived.status_code == 200 and archived.json()["archived_at"]
    assert (
        client.post(f"/v1/projects/{PROJECT}/signal-keys/kb.search.nope/archive", headers=hub["owner"]).status_code
        == 404
    )
    done = push(client, hub["alice"], {"key": "kb.search.latency_ms", "value": 120})
    assert done["accepted"] == 0 and "archived" in done["rejected"][0]["reason"]
    key(client, hub["owner"], "kb.search.latency_ms", "number", expect=409)  # archived, the name stays taken
    names = [
        k["name"] for k in client.get(f"/v1/projects/{PROJECT}/signal-keys?archived=true", headers=hub["eve"]).json()
    ]
    assert "kb.search.latency_ms" in names
    assert "kb.search.latency_ms" not in [
        k["name"] for k in client.get(f"/v1/projects/{PROJECT}/signal-keys", headers=hub["eve"]).json()
    ]
    audited = sql(
        hub_db, select(tables.audit.c.action, tables.audit.c.target).where(tables.audit.c.action.like("signal.key%"))
    )
    assert ("signal.key-archive", f"{PROJECT} key=kb.search.latency_ms") in audited


# Reading


def test_members_read_signals_by_key_and_time_under_their_labels(client, hub):
    service = bearer(ingest_key(client, hub["owner"])["token"])
    push(
        client,
        service,
        {"key": ZERO_RESULTS, "value": 0.30, "measured_at": at(180), "tenant": "northwind"},
        {"key": ZERO_RESULTS, "value": 0.20, "measured_at": at(120), "tenant": "lumen-shop", "level": "public"},
        {"key": ZERO_RESULTS, "value": 0.10, "measured_at": at(60), "tenant": "northwind"},
        {"key": TICKETS, "text": "Search finds nothing for 'refund'", "measured_at": at(30), "level": "public"},
    )
    newest = read(client, hub["eve"])
    assert [s["key"] for s in newest] == [TICKETS, ZERO_RESULTS, ZERO_RESULTS, ZERO_RESULTS]
    assert newest[0]["text"] == "Search finds nothing for 'refund'" and newest[0]["kind"] == "feedback"
    assert newest[0]["ingest_key"] == "northwind-kb" and newest[0]["login"] is None
    ranged = read(client, hub["eve"], key=ZERO_RESULTS, since=at(150), until=at(45))
    assert [s["value"] for s in ranged] == [0.10, 0.20]
    assert [s["value"] for s in read(client, hub["eve"], key=ZERO_RESULTS, tenant="northwind")] == [0.10, 0.30]
    assert [s["value"] for s in read(client, hub["eve"], key=ZERO_RESULTS, limit=1)] == [0.10]
    public = read(client, hub["outsider"])  # a grant up to public reads the public signals alone
    assert [(s["key"], s["label"]["level"]) for s in public] == [(TICKETS, "public"), (ZERO_RESULTS, "public")]
    assert read(client, hub["eve"], key="kb.search.nobody") == []
    read(client, hub["eve"], expect=422, limit=0)
    outsider_of_lumen = client.get(f"/v1/projects/{OTHER}/signals", headers=hub["eve"])
    assert outsider_of_lumen.status_code == 404


# The retention


def test_the_retention_prunes_signals_measured_before_its_days(client, hub, hub_db):
    push(client, hub["alice"], {"key": ZERO_RESULTS, "value": 0.5}, {"key": ZERO_RESULTS, "value": 0.6})
    s = tables.signals
    first = sql(hub_db, select(func.min(s.c.id)))[0][0]
    sql(hub_db, update(s).where(s.c.id == first).values(measured_at=func.now() - timedelta(days=401)))
    state = client.app.state
    assert client.portal.call(signals.prune_signals, state.engine, 400) == {"deleted": 1, "days": 400}
    assert sql(hub_db, select(func.count()).select_from(s)) == [(1,)]
    assert client.portal.call(signals.prune_signals, state.engine, 400) == {"deleted": 0, "days": 400}


def test_the_signal_pruning_runs_daily_once_and_its_days_are_configured():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.PRUNE_SIGNALS].cron == "3 5 * * *"
    assert queue.tasks[jobs.PRUNE_SIGNALS].queueing_lock == jobs.PRUNE_SIGNALS
    base = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    assert load_config(base).signal_days == 400
    assert load_config({**base, "EVO_HUB_SIGNAL_DAYS": "90"}).signal_days == 90
    for bad in ("29", "3651", "forever"):
        with pytest.raises(ConfigError, match="EVO_HUB_SIGNAL_DAYS"):
            load_config({**base, "EVO_HUB_SIGNAL_DAYS": bad})


def test_signal_rows_carry_who_pushed_them_and_refuse_both_or_neither(hub_db, client, hub):
    """The CHECK of 0068 holds what the routes promise, for a row written around them."""
    import psycopg

    push(client, hub["alice"], {"key": ZERO_RESULTS, "value": 0.5})
    s = tables.signals
    names = ("project_id", "key_id", "user_id", "batch_id", "label")
    [row] = sql(hub_db, select(*(s.c[name] for name in names)))
    base = dict(zip(names, row, strict=True)) | {"measured_at": func.now(), "via": "member"}
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, insert(s).values(**base))  # neither a value nor a text
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, insert(s).values(**base | {"value": 1.0, "via": "ingest"}))  # an ingest key's row names its key
