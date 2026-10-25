"""The outcome of a task as a field of its own (criterion a13 of plan pm-daily-tracking): the API sets it on create and
edit and records the change in the task's history. The pm daily report, which reads it before the section
``## Kết quả`` or ``## Outcome`` of the description, is the core plugin pm's, and evo-agents-core-plugins tests it."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_tasks_plugin import (  # noqa: F401 (fixtures)
    client,
    created,
    edited,
    history,
    hub,
    members,
    shown,
    tasks_core,
)


def test_task_outcome_is_set_on_create_and_edit_and_edits_write_one_history_row(client, hub):
    alice = hub["alice"]
    task = created(client, alice, title="Cancel", outcome_md="Orders cancel in one tap.")
    assert task["outcome_md"] == "Orders cancel in one tap." and task["description_md"] == ""
    assert shown(client, alice, task["ref"])["outcome_md"] == "Orders cancel in one tap."
    plain = created(client, alice, title="Plain")
    assert plain["outcome_md"] == ""
    changed = edited(client, hub["bao"], task["ref"], outcome_md="Shipped.")
    assert changed["outcome_md"] == "Shipped."
    assert history(changed, "outcome") == [("bao", None, None)]
    again = edited(client, hub["bao"], task["ref"], outcome_md="Shipped.", title="Cancel it")
    assert len(history(again, "outcome")) == 1  # the same text writes no row
    cleared = edited(client, hub["bao"], task["ref"], outcome_md="")
    assert cleared["outcome_md"] == "" and len(history(cleared, "outcome")) == 2
    edited(client, alice, task["ref"], expect=422, outcome_md="x" * 100_001)
