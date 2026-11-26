"""A decision's label (``worker ask --label``, decisions.label): the agent gives a level of its project's ladder at most
the run's maximum, the hub refuses any other, and every view shows the effective label, its own or the project's
default label. ``docs/notifications.md`` (Decisions on channels) is the design; the channels' side is
``tests/plugins/test_decision_channels.py``.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.server.decisions.service.channels import contributed_kinds
from tests.hub.conftest import github, hub_db, hub_template, pg_server  # noqa: F401 (fixtures)
from tests.hub.live import sql
from tests.hub.test_decisions import ask, asked, client, decision, hub  # noqa: F401 (fixtures)
from tests.hub.test_plan_runs import started


def stored_label(db, decision_id: int):
    d = tables.decisions
    return sql(db, select(d.c.label).where(d.c.id == decision_id))[0][0]


def test_a_decision_asked_with_a_label_keeps_it_and_every_view_shows_it(client, hub, hub_db):
    worker, run = started(client, hub)
    found = asked(client, worker, run["id"], label="internal")
    assert (found["label"], found["label_given"]) == ("internal", True)
    # no channel is linked: one entry not sent for each kind the installed core plugins contribute, none without them
    not_linked = [(d["channel"], d["state"], d["reason"]) for d in found["channel_deliveries"]]
    assert not_linked == [
        (kind, "not_sent", f"{found['asked_of']} has not linked {kind}") for kind in contributed_kinds()
    ]
    assert stored_label(hub_db, found["id"]) == "internal"
    assert decision(client, hub["reader"], found["id"])["label"] == "internal"


def test_a_decision_asked_without_a_label_reads_as_its_projects_default_label(client, hub, hub_db):
    worker, run = started(client, hub)
    found = asked(client, worker, run["id"])
    assert stored_label(hub_db, found["id"]) is None
    p = tables.projects
    ((default,),) = sql(hub_db, select(p.c.default_label).where(p.c.name == found["project"]))
    assert (found["label"], found["label_given"]) == (default["level"], False)


def test_a_label_the_ladder_lacks_or_above_the_runs_maximum_gets_422_and_asks_nothing(client, hub, hub_db):
    worker, run = started(client, hub)
    unknown = ask(client, worker, run["id"], label="top-secret")
    assert unknown.status_code == 422
    assert "is not a level of project" in unknown.json()["message"]
    above = ask(client, worker, run["id"], label="customer")  # the owner's grant reaches internal
    assert above.status_code == 422
    assert "above what the run reaches" in above.json()["message"]
    assert ask(client, worker, run["id"], label="").status_code == 422
    assert sql(hub_db, select(tables.decisions.c.id)) == []
