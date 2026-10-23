"""The core plugin crm on the hub (``evo_agents.core_plugins.crm``): contacts who found the project themselves.

The job ``intake`` finds them in the invites and grants of the hub and in the issues, pull requests and discussions of
the project's repo (a fake fetcher stands for GitHub: nothing here calls out), computes their stages (lead, invited,
activated, retained) from hub data and keeps them with history, and pushes the stage counts to Signal as ``crm.*``.
A replier, a team member and a bot are no contacts. Readers read, writers note, admins delete; a deletion removes the
touchpoints and leaves no personal data in the audit. A project that did not enable the plugin writes nothing and its
routes answer 404. The graph source gives login, source and stage alone.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, null, select, update

from evo_agents.core_plugins.crm import MANIFEST, job, rules
from evo_agents.core_plugins.crm import kg as crm_kg
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext
from tests.hub.live import sql
from tests.hub.test_runs import dispatched
from tests.hub.test_tasks_plugin import (  # noqa: F401 (fixtures)
    PROJECT,
    client,
    configure,
    enable,
    members,
    tasks_core,
)

BASE = f"/v1/x/crm/{PROJECT}/contacts"
REPO = "acme/widgets"
NOW = datetime.now(UTC)


class Fake:
    """A GitHub that answers what it was given, the way the REST list does: items updated at or after ``since``."""

    def __init__(self, issues=(), discussions=()):
        self.found, self.talks, self.fail = list(issues), list(discussions), False

    async def issues(self, repo, since):
        if self.fail:
            raise OSError("no network")
        assert repo == REPO
        return [i for i in self.found if since is None or datetime.fromisoformat(i["updated_at"]) >= since]

    async def discussions(self, repo, since):
        return list(self.talks)


def issue(number: int, login: str, association: str = "NONE", when: datetime | None = None, **extra) -> dict:
    stamp = (when or NOW - timedelta(days=2)).isoformat()
    return {
        "number": number,
        "title": extra.pop("title", "Question"),
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "user": {"login": login, "type": extra.pop("type", "User")},
        "author_association": association,
        "created_at": stamp,
        "updated_at": stamp,
        **extra,
    }


def talk(number: int, login: str) -> dict:
    stamp = (NOW - timedelta(days=1)).isoformat()
    return {
        "number": number,
        "url": f"https://github.com/{REPO}/discussions/{number}",
        "createdAt": stamp,
        "updatedAt": stamp,
        "author": {"login": login, "__typename": "User"},
        "authorAssociation": "NONE",
    }


@pytest.fixture
def crm(client, members, hub_db):  # noqa: F811
    enable(client, members["owner"], "crm")
    configure(client, members["owner"], "crm", {"repo": REPO})
    dispatched(client, members["owner"], [2])
    return members


def run(client, fetcher=None, project: str = PROJECT) -> dict:
    engine = client.app.state.engine
    return client.portal.call(lambda: job.run_project(engine, project, fetcher=fetcher or Fake()))


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def listed(client, headers, **query) -> dict:
    answer = client.get(BASE, params=query, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def stages(client, headers) -> dict[str, str]:
    return {c["login"]: c["stage"] for c in listed(client, headers)["contacts"]}


def shown(client, headers, login: str) -> dict:
    answer = client.get(f"{BASE}/{login}", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def invite(db, owner: str, **values) -> None:
    sql(
        db,
        insert(tables.invites).values(
            kind="join",
            project_id=project_id(db),
            role="reader",
            max_level="internal",
            token_hash=hashlib.sha256(repr(sorted(values.items())).encode()).hexdigest(),
            created_by=user_id(db, owner),
            expires_at=NOW + timedelta(days=7),
            **values,
        ),
    )


def dispatch(db, login: str, queued_at: datetime) -> None:
    """A run of ``login`` queued at ``queued_at``: a copy of the run the fixture dispatched, with these two changed."""
    r = tables.runs
    template = sql(db, select(r).order_by(r.c.id).limit(1))[0]
    values = dict(zip(r.c.keys(), template, strict=True))
    values.pop("id")
    values = {name: null() if value is None else value for name, value in values.items()}  # not JSON null
    sql(
        db,
        insert(r).values(
            **{**values, "dispatched_by": user_id(db, login), "queued_at": queued_at, "step_key": uuid4().hex[:8]}
        ),
    )


def declare(db, owner: str) -> None:
    keys = [
        {"project_id": project_id(db), "name": f"crm.{s}", "kind": "number", "created_by": user_id(db, owner)}
        for s in rules.STAGES
    ]
    sql(db, insert(tables.signal_keys), keys)


def counts(db) -> dict[str, list[float]]:
    s, k = tables.signals, tables.signal_keys
    rows = sql(db, select(k.c.name, s.c.value).join_from(s, k, k.c.id == s.c.key_id).order_by(s.c.id))
    found: dict[str, list[float]] = {}
    for name, value in rows:
        found.setdefault(name, []).append(value)
    return found


# Manifest


def test_crm_manifest_declares_no_permission_no_page_and_the_five_tables():
    contributes = MANIFEST["contributes"]
    assert MANIFEST["id"] == "crm" and MANIFEST["permissions"] == [] and "pages" not in contributes
    assert [j["name"] for j in contributes["jobs"]] == ["intake"] and contributes["jobs"][0]["cron"]
    assert {t["name"] for t in contributes["tables"]} == {
        "crm_contacts",
        "crm_touchpoints",
        "crm_stage_history",
        "crm_cursors",
        "crm_deletions",
    }
    assert [k["name"] for k in contributes["kg_sources"]] == ["contacts"]


# Sources


def test_crm_the_hub_gives_contacts_from_invites_and_grants_and_the_team_is_left_out(client, crm, hub_db):
    invite(hub_db, "owner", for_login="carol")
    invite(hub_db, "owner", accepted_by=user_id(hub_db, "alice"), accepted_at=NOW)
    done = run(client)
    assert done["hub"]["contacts"] >= 4
    found = stages(client, crm["alice"])
    assert "owner" not in found, "an admin of the project is the team"
    assert set(found) == {"alice", "bao", "eve", "outsider", "carol"}
    assert shown(client, crm["alice"], "carol")["source"] == "invite_link"
    assert {t["kind"] for t in shown(client, crm["alice"], "alice")["touchpoints"]} == {"invite_link", "grant"}
    assert shown(client, crm["alice"], "bao")["source"] == "grant"
    assert shown(client, crm["alice"], "bao")["profile_url"] == "https://github.com/bao"


def test_crm_github_gives_contacts_from_the_issue_form_issues_pull_requests_and_discussions(client, crm):
    fetcher = Fake(
        [
            issue(1, "Dung", title="Invite: let me in"),
            issue(2, "Giang"),
            issue(3, "Hoa", pull_request={"url": "x"}),
            issue(4, "owner", "OWNER"),
            issue(5, "teammate", "MEMBER"),
            issue(6, "robot", type="Bot"),
        ],
        [talk(7, "Khanh")],
    )
    run(client, fetcher)
    contacts = {c["login"]: c["source"] for c in listed(client, crm["eve"])["contacts"]}
    assert contacts["dung"] == "invite_request" and contacts["giang"] == "issue"
    assert contacts["hoa"] == "pull_request" and contacts["khanh"] == "discussion"
    assert not {"owner", "teammate", "robot"} & set(contacts)
    assert shown(client, crm["eve"], "dung")["touchpoints"][0]["url"].endswith("/issues/1")


def test_crm_a_replier_is_not_a_contact_and_a_second_pass_adds_nothing(client, crm, hub_db):
    fetcher = Fake([issue(1, "Dung")])
    run(client, fetcher)
    first = sql(hub_db, select(func.count()).select_from(tables.crm_touchpoints))[0][0]
    fetcher.found.append({**issue(1, "Replier"), "number": 99, "pull_request": None} | {"user": {"login": "x" * 50}})
    run(client, fetcher)
    run(client, fetcher)
    assert sql(hub_db, select(func.count()).select_from(tables.crm_touchpoints))[0][0] == first
    assert "replier" not in stages(client, crm["eve"])


def test_crm_a_failed_github_read_changes_nothing_and_the_hub_sources_still_count(client, crm):
    fetcher = Fake([issue(1, "Dung")])
    fetcher.fail = True
    done = run(client, fetcher)
    assert (
        done["github"]["failed"] == 1
        and "dung" not in stages(client, crm["eve"])
        and "bao" in stages(client, crm["eve"])
    )
    fetcher.fail = False
    run(client, fetcher)
    assert "dung" in stages(client, crm["eve"])


# Stages


def test_crm_stages_are_lead_invited_activated_retained_with_history_and_never_go_down(client, crm, hub_db):
    run(client, Fake([issue(1, "Dung"), issue(2, "Giang")]))
    assert stages(client, crm["eve"]) == {
        "dung": "lead",
        "giang": "lead",
        "alice": "invited",
        "bao": "invited",
        "eve": "invited",
        "outsider": "invited",
    }
    dispatch(hub_db, "alice", NOW - timedelta(days=40))
    dispatch(hub_db, "bao", NOW - timedelta(days=40))
    dispatch(hub_db, "bao", NOW - timedelta(days=40) + timedelta(days=22))  # in the window of days 21 to 28
    dispatch(hub_db, "eve", NOW - timedelta(days=40))
    dispatch(hub_db, "eve", NOW - timedelta(days=40) + timedelta(days=30))  # too late: no retention
    run(client, Fake([issue(1, "Dung"), issue(2, "Giang")]))
    found = stages(client, crm["eve"])
    assert (found["alice"], found["bao"], found["eve"], found["outsider"]) == (
        "activated",
        "retained",
        "activated",
        "invited",
    )
    bao = shown(client, crm["eve"], "bao")
    assert [h["stage"] for h in bao["history"]] == ["lead", "invited", "activated", "retained"]
    at = {h["stage"]: datetime.fromisoformat(h["reached_at"]) for h in bao["history"]}
    assert at["retained"] - at["activated"] == timedelta(days=22), "the time of a stage is that of its fact"
    sql(hub_db, update(tables.runs).where(tables.runs.c.dispatched_by == user_id(hub_db, "bao")).values(queued_at=NOW))
    run(client, Fake())
    assert stages(client, crm["eve"])["bao"] == "retained", "a stage kept never goes down"
    assert [h["stage"] for h in shown(client, crm["eve"], "bao")["history"]] == [
        "lead",
        "invited",
        "activated",
        "retained",
    ]


def test_crm_the_counts_go_to_signal_under_crm_keys_only_once_an_admin_declared_them(client, crm, hub_db):
    done = run(client, Fake([issue(1, "Dung")]))
    assert done["signals"]["pushed"] and done["signals"]["rejected"] == 4 and counts(hub_db) == {}
    declare(hub_db, "owner")
    run(client, Fake([issue(1, "Dung")]))
    assert counts(hub_db) == {"crm.lead": [1.0], "crm.invited": [4.0], "crm.activated": [0.0], "crm.retained": [0.0]}
    again = run(client, Fake([issue(1, "Dung")]))
    assert again["signals"]["accepted"] == 0 and again["signals"]["duplicates"] == 4
    dispatch(hub_db, "alice", NOW - timedelta(days=1))
    run(client, Fake([issue(1, "Dung")]))
    assert counts(hub_db)["crm.activated"] == [0.0, 1.0] and counts(hub_db)["crm.invited"] == [4.0, 3.0]
    assert "dung" not in json.dumps(
        sql(hub_db, select(tables.signals.c.text, tables.signals.c.source, tables.signals.c.tenant))
    )


# A project without the plugin


def test_crm_a_project_that_does_not_enable_it_writes_nothing_and_its_routes_answer_404(client, members, hub_db):
    assert run(client, Fake([issue(1, "Dung")])) == {}
    for table in (tables.crm_contacts, tables.crm_touchpoints, tables.crm_cursors, tables.crm_stage_history):
        assert sql(hub_db, select(func.count()).select_from(table))[0][0] == 0
    assert sql(hub_db, select(func.count()).select_from(tables.signal_ingest_keys))[0][0] == 0
    assert client.get(BASE, headers=members["alice"]).status_code == 404
    assert client.get(f"{BASE}/alice", headers=members["owner"]).status_code == 404
    assert client.post(f"{BASE}/alice/notes", json={"note": "x"}, headers=members["owner"]).status_code == 404
    assert client.delete(f"{BASE}/alice", headers=members["owner"]).status_code == 404
    assert run(client, Fake(), project="no-such-project") == {}


# Roles and labels


def test_crm_a_reader_reads_a_writer_notes_and_only_an_admin_deletes(client, crm):
    run(client, Fake([issue(1, "Dung")]))
    assert client.post(f"{BASE}/dung/notes", json={"note": "x"}, headers=crm["eve"]).status_code == 403
    noted = client.post(f"{BASE}/dung/notes", json={"note": "met at a meetup"}, headers=crm["alice"])
    assert noted.status_code == 201, noted.text
    assert (noted.json()["kind"], noted.json()["note"], noted.json()["author"]) == ("note", "met at a meetup", "alice")
    assert client.post(f"{BASE}/dung/notes", json={"note": ""}, headers=crm["alice"]).status_code == 422
    assert client.post(f"{BASE}/dung/notes", json={"note": "x" * 1001}, headers=crm["alice"]).status_code == 422
    assert [t["note"] for t in shown(client, crm["eve"], "dung")["touchpoints"] if t["kind"] == "note"] == [
        "met at a meetup"
    ]
    assert client.delete(f"{BASE}/dung", headers=crm["eve"]).status_code == 403
    assert client.delete(f"{BASE}/dung", headers=crm["alice"]).status_code == 403
    assert client.get(f"{BASE}/nobody", headers=crm["eve"]).status_code == 404
    assert client.get(BASE, headers={"Authorization": "Bearer nope"}).status_code in (401, 403)


def test_crm_a_note_does_not_make_a_contact_a_lead_or_move_its_stage(client, crm):
    run(client, Fake([issue(1, "Dung")]))
    client.post(f"{BASE}/dung/notes", json={"note": "hello"}, headers=crm["alice"])
    run(client, Fake([issue(1, "Dung")]))
    assert stages(client, crm["eve"])["dung"] == "lead"


def test_crm_the_list_filters_by_stage_and_source_and_a_label_above_the_grant_hides_a_contact(client, crm, hub_db):
    run(client, Fake([issue(1, "Dung"), issue(2, "Giang", title="Invite: me")]))
    assert {c["login"] for c in listed(client, crm["eve"], stage="lead")["contacts"]} == {"dung", "giang"}
    assert [c["login"] for c in listed(client, crm["eve"], source="invite_request")["contacts"]] == ["giang"]
    assert client.get(BASE, params={"stage": "gold"}, headers=crm["eve"]).status_code == 422
    assert listed(client, crm["eve"])["counts"] == {"lead": 2, "invited": 4, "activated": 0, "retained": 0}
    sql(
        hub_db,
        update(tables.crm_contacts)
        .where(tables.crm_contacts.c.login == "dung")
        .values(label={"level": "restricted", "location": "any"}),
    )
    blind = listed(client, crm["eve"])
    assert "dung" not in {c["login"] for c in blind["contacts"]} and blind["counts"]["lead"] == 1
    assert client.get(f"{BASE}/dung", headers=crm["eve"]).status_code == 404
    assert listed(client, crm["outsider"])["contacts"] == []  # public up to: the default label is internal


# Deletion on request


def test_crm_a_deletion_removes_the_touchpoints_and_the_audit_and_the_record_hold_no_personal_data(client, crm, hub_db):
    fetcher = Fake([issue(1, "Dung"), issue(8, "Dung")])
    run(client, fetcher)
    client.post(f"{BASE}/dung/notes", json={"note": "private words"}, headers=crm["alice"])
    gone = client.delete(f"{BASE}/dung", headers=crm["owner"])
    assert gone.status_code == 200 and gone.json() == {"deleted": True, "touchpoints": 3}
    assert (
        "dung" not in stages(client, crm["eve"]) and client.get(f"{BASE}/dung", headers=crm["eve"]).status_code == 404
    )
    gone_id = sql(hub_db, select(tables.crm_deletions.c.contact_id))[0][0]
    for table in (tables.crm_touchpoints, tables.crm_stage_history):
        assert sql(hub_db, select(func.count()).select_from(table).where(table.c.contact_id == gone_id)) == [(0,)]
    contact = sql(
        hub_db, select(func.count()).select_from(tables.crm_contacts).where(tables.crm_contacts.c.login == "dung")
    )
    assert contact == [(0,)]
    deletions = sql(hub_db, select(tables.crm_deletions))
    audit = sql(hub_db, select(tables.audit).where(tables.audit.c.action == "crm.delete"))
    assert len(deletions) == 1 and len(audit) == 1
    dump = json.dumps([[str(x) for x in row] for row in deletions + audit])
    assert "dung" not in dump.lower() and "private words" not in dump
    assert client.delete(f"{BASE}/dung", headers=crm["owner"]).status_code == 404
    run(client, fetcher)  # what GitHub still says does not make the contact again
    assert "dung" not in stages(client, crm["eve"])
    assert sql(
        hub_db,
        select(func.count()).select_from(tables.crm_touchpoints).where(tables.crm_touchpoints.c.ref == "issue#8"),
    ) == [(0,)]


# Graph


def test_crm_the_graph_source_gives_login_source_and_stage_alone(client, crm, hub_db):
    run(client, Fake([issue(1, "Dung")]))
    client.post(f"{BASE}/dung/notes", json={"note": "private words"}, headers=crm["alice"])
    context = CoreContext("crm", client.app.state.engine, None, PROJECT, project_id(hub_db))
    answered = client.portal.call(lambda: crm_kg.kg_source({"source": "plugin.crm"}, context))
    items = {i["title"]: i for i in answered["items"]}
    assert set(items) == {"dung", "alice", "bao", "eve", "outsider"}
    item = items["dung"]
    assert item["id"] == "plugin.crm:contact:dung" and item["props"] == {
        "login": "dung",
        "source": "issue",
        "stage": "lead",
    }
    assert "private words" not in json.dumps(answered) and "github.com" not in json.dumps(item)
    assert item["label"]["level"]
