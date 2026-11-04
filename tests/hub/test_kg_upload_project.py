"""A project made from an upload (plan kg-upload-archive, step 3): ``hub kg upload FILE --new-project P [--label L]``
creates P with no harness (the default ladder, a hub sink that clears L, a Claude Code sink that clears only
``internal``, a minimal knowledge config, its creator as admin) and uploads into it; it takes what registering a
project takes, spends a register right as registering does, refuses a name the hub holds, and the first build that
follows queues a kg_init run with ``first_build``.

The api and the worker run in this process and the blob store is moto's, as in ``tests.hub.test_kg``."""

from __future__ import annotations

import argparse
import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select

from evo_agents.hub import kg_upload_cli, tables
from evo_agents.hub.client import HubError
from evo_agents.hub.kg_ingest import config_problems
from evo_agents.hub.server import kg_upload_project
from evo_agents.kg.policy import DEFAULT_LEVELS, DEFAULT_LOCATIONS
from tests.hub import live
from tests.hub.test_kg import PROJECT, InProcessHub, query, run_jobs
from tests.hub.test_kg_upload_extract import extract, hub  # noqa: F401 (hub is a fixture)

NEW = "lumen-data"
SENTENCE = "The lantern ferry leaves the north pier at dawn."


def zip_of(files: dict[str, bytes], path: Path) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as packed:
        for name, data in files.items():
            packed.writestr(name, data)
    path.write_bytes(buffer.getvalue())
    return path


def notes(tmp_path: Path) -> Path:
    return zip_of({"notes/ferry.md": f"# Ferry\n\n{SENTENCE}\n".encode()}, tmp_path / "ferry-notes.zip")


def send(hub, headers, path: Path, project: str = NEW, label: str | None = None) -> dict:
    done, _, _ = kg_upload_cli.upload(InProcessHub(hub.client, headers), project, path, label=label, new_project=True)
    return done


def project_row(hub, name: str = NEW):
    p = tables.projects
    found = query(hub.db, select(p.c.id, p.c.levels, p.c.locations, p.c.default_label).where(p.c.name == name))
    return found[0] if found else None


def sinks_of(hub, project_id: int) -> dict:
    s = tables.project_sinks
    held = query(hub.db, select(s.c.sink_id, s.c.kind, s.c.clearance).where(s.c.project_id == project_id))
    return {sink: (kind, clearance) for sink, kind, clearance in held}


def grant_of(hub, project_id: int, login: str):
    g, users = tables.grants, tables.users
    found = query(
        hub.db,
        select(g.c.role, g.c.max_level)
        .join_from(g, users, users.c.id == g.c.user_id)
        .where(g.c.project_id == project_id, users.c.login == login),
    )
    return found[0] if found else None


def register_right(hub, login: str) -> tuple[dict, int]:
    """The Authorization header of ``login``, who accepted a register invite link; the invite's id."""
    headers = live.bearer(live.insert_token(hub.db, login))
    users, invites = tables.users, tables.invites
    ((user_id,),) = query(hub.db, select(users.c.id).where(users.c.login == login))
    ((admin_id,),) = query(hub.db, select(users.c.id).where(users.c.login == live.ADMIN))
    now = datetime.now(UTC)
    row = {
        "kind": "register",
        "token_hash": hashlib.sha256(login.encode()).hexdigest(),
        "created_by": admin_id,
        "expires_at": now + timedelta(days=7),
        "accepted_at": now,
        "accepted_by": user_id,
    }
    ((invite_id,),) = query(hub.db, insert(invites).values(row).returning(invites.c.id))
    return headers, invite_id


def invite_use(hub, invite_id: int):
    i = tables.invites
    ((used_at, project_id),) = query(hub.db, select(i.c.used_at, i.c.project_id).where(i.c.id == invite_id))
    return used_at, project_id


def test_a_hub_admin_makes_a_project_from_an_upload_with_the_ladder_sinks_config_and_admin_grant(hub, tmp_path):
    done = send(hub, hub.admin, notes(tmp_path), label="customer")
    assert done["state"] == "extracting" and done["label"]["level"] == "customer"
    project_id, levels, locations, default_label = project_row(hub)
    assert (levels, locations) == (DEFAULT_LEVELS, DEFAULT_LOCATIONS)
    assert default_label["level"] == "customer"
    assert sinks_of(hub, project_id) == {
        "evo-hub": ("hub", {"level": "customer"}),
        "claude-code@anthropic": ("agent-session", {"level": "internal"}),
    }, "the hub sink clears L; the agent's session clears internal alone"
    assert grant_of(hub, project_id, live.ADMIN) == ("admin", DEFAULT_LEVELS[-1])
    c = tables.kg_configs
    ((knowledge, ontology),) = query(hub.db, select(c.c.knowledge, c.c.ontology).where(c.c.project_id == project_id))
    config = {"knowledge": knowledge, "ontology": ontology}
    assert config == kg_upload_project.knowledge_config(NEW, "customer")
    assert config_problems(NEW, DEFAULT_LEVELS, DEFAULT_LOCATIONS, config) == [], "the build can use it"
    shown = hub.client.get(f"/v1/projects/{NEW}", headers=hub.admin).json()
    assert shown["harness"] is None and shown["role"] == "admin"
    audit = tables.audit
    targets = query(hub.db, select(audit.c.action, audit.c.target).where(audit.c.action == "project.register"))
    assert (("project.register", NEW)) in targets


def test_without_a_label_the_new_project_and_its_hub_sink_are_internal(hub, tmp_path):
    done = send(hub, hub.admin, notes(tmp_path))
    assert done["label"]["level"] == kg_upload_project.DEFAULT_LEVEL == "internal"
    project_id = project_row(hub)[0]
    assert sinks_of(hub, project_id)["evo-hub"] == ("hub", {"level": "internal"})


def test_a_member_needs_a_register_right_and_spends_it(hub, tmp_path):
    with pytest.raises(HubError) as refused:
        send(hub, hub.alice, notes(tmp_path))
    assert refused.value.status == 403 and project_row(hub) is None, "a writer of another project creates nothing"

    founder, invite_id = register_right(hub, "founder")
    done = send(hub, founder, notes(tmp_path), label="public")
    project_id = project_row(hub)[0]
    assert done["state"] == "extracting"
    assert grant_of(hub, project_id, "founder") == ("admin", DEFAULT_LEVELS[-1]), "the creator is its admin"
    used_at, used_for = invite_use(hub, invite_id)
    assert used_at is not None and used_for == project_id, "the right is spent as registering spends it"
    with pytest.raises(HubError) as again:
        send(hub, founder, notes(tmp_path), project="lumen-other")
    assert again.value.status == 403 and project_row(hub, "lumen-other") is None
    # the founder keeps uploading into their project with --project, as its admin
    more = zip_of({"more.md": b"# More\n"}, tmp_path / "more.zip")
    kept, _, _ = kg_upload_cli.upload(InProcessHub(hub.client, founder), NEW, more, label="public")
    assert kept["state"] == "extracting"


def test_an_existing_name_is_refused_and_nothing_is_changed_or_spent(hub, tmp_path):
    before = project_row(hub, PROJECT)
    sinks = sinks_of(hub, before[0])
    with pytest.raises(HubError) as hidden:
        send(hub, hub.alice, notes(tmp_path), project=PROJECT)
    assert hidden.value.status == 403, "without the right, a name taken or not answers the same"
    founder, invite_id = register_right(hub, "founder")
    for headers in (hub.admin, founder):
        with pytest.raises(HubError) as refused:
            send(hub, headers, notes(tmp_path), project=PROJECT, label="public")
        assert refused.value.status == 409 and "exists already" in str(refused.value)
    assert project_row(hub, PROJECT) == before and sinks_of(hub, before[0]) == sinks
    assert invite_use(hub, invite_id) == (None, None), "the right stays unused"
    assert query(hub.db, select(tables.kg_uploads.c.id)) == []


def test_a_label_off_the_default_ladder_is_refused_before_anything(hub, tmp_path):
    with pytest.raises(HubError) as refused:
        send(hub, hub.admin, notes(tmp_path), label="topsecret")
    assert refused.value.status == 422 and project_row(hub) is None


def test_the_first_build_after_the_upload_queues_a_kg_init_run_with_first_build(hub, tmp_path):
    done = send(hub, hub.admin, notes(tmp_path), label="public")
    assert extract(hub) == {"extracted": 1}
    u = tables.kg_uploads
    ((build_id,),) = query(hub.db, select(u.c.build_id).where(u.c.id == done["id"]))
    assert build_id is not None, "the minimal config lets the extraction queue a build"
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    b = tables.kg_builds
    project_id = project_row(hub)[0]
    ((status,),) = query(hub.db, select(b.c.status).where(b.c.id == build_id, b.c.project_id == project_id))
    assert status == "succeeded"
    t = tables.kg_init_runs
    runs = query(hub.db, select(t.c.via, t.c.build_id).where(t.c.project_id == project_id))
    assert runs == [("first_build", build_id)]
    found = hub.client.post(
        f"/v1/kg/{NEW}/tools/kg_search",
        json={"arguments": {"query": SENTENCE}, "sink": kg_upload_project.AGENT_SINK},
        headers=hub.admin,
    )
    assert found.status_code == 200, found.text
    ids = [str(node.get("id")) for node in found.json()["structuredContent"]["results"]]
    assert any("notes/ferry.md" in node for node in ids), ids


def test_the_command_takes_project_or_new_project_never_both(monkeypatch):
    parser = argparse.ArgumentParser()
    kg_upload_cli.register(parser.add_subparsers())
    args = parser.parse_args(["upload", "a.zip", "--new-project", NEW, "--label", "public"])
    assert (args.new_project, args.project, args.label) == (NEW, None, "public")
    for wrong in (["upload", "a.zip"], ["upload", "a.zip", "--project", "x", "--new-project", "y"]):
        with pytest.raises(SystemExit):
            parser.parse_args(wrong)
