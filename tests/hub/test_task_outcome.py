"""The outcome of a task as a field of its own (criterion a13 of plan pm-daily-tracking): the API sets it on create and
edit and records the change in the task's history, and the pm daily report reads it before the section
``## Kết quả`` or ``## Outcome`` of the description, which it falls back to while the field is empty."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_pm_daily_plugin import client, daily, pm_core, pm_hub, tomorrow  # noqa: F401 (fixtures)
from tests.hub.test_tasks_plugin import (  # noqa: F401 (fixtures)
    created,
    edited,
    history,
    hub,
    members,
    shown,
)

SECTION = "Why\n\n## Outcome\nFrom the description."


def outcome_of(client, headers, code: str) -> str | None:
    got = daily(client, headers, date=tomorrow())
    return {case["code"]: case["outcome"] for case in got["done_use_cases"]}[code]


def test_task_outcome_is_set_on_create_and_edit_and_edits_write_one_history_row(client, pm_hub):
    alice = pm_hub["alice"]
    task = created(client, alice, title="Cancel", outcome_md="Orders cancel in one tap.")
    assert task["outcome_md"] == "Orders cancel in one tap." and task["description_md"] == ""
    assert shown(client, alice, task["ref"])["outcome_md"] == "Orders cancel in one tap."
    plain = created(client, alice, title="Plain")
    assert plain["outcome_md"] == ""
    changed = edited(client, pm_hub["bao"], task["ref"], outcome_md="Shipped.")
    assert changed["outcome_md"] == "Shipped."
    assert history(changed, "outcome") == [("bao", None, None)]
    again = edited(client, pm_hub["bao"], task["ref"], outcome_md="Shipped.", title="Cancel it")
    assert len(history(again, "outcome")) == 1  # the same text writes no row
    cleared = edited(client, pm_hub["bao"], task["ref"], outcome_md="")
    assert cleared["outcome_md"] == "" and len(history(cleared, "outcome")) == 2
    edited(client, alice, task["ref"], expect=422, outcome_md="x" * 100_001)


def test_task_outcome_comes_before_the_section_of_the_description_in_the_pm_report(client, pm_hub):
    alice, owner = pm_hub["alice"], pm_hub["owner"]
    case = {"use_cases": ["UC ORD-1"]}
    task = created(client, alice, title="Cancel", description_md=SECTION, links=case)
    edited(client, alice, task["ref"], status="done")
    assert outcome_of(client, owner, "UC ORD-1") == "From the description."  # field empty: the section
    edited(client, alice, task["ref"], outcome_md="  Orders cancel in one tap.\n")
    assert outcome_of(client, owner, "UC ORD-1") == "Orders cancel in one tap."  # field first
    edited(client, alice, task["ref"], outcome_md="")
    assert outcome_of(client, owner, "UC ORD-1") == "From the description."
    edited(client, alice, task["ref"], description_md="no section")
    assert outcome_of(client, owner, "UC ORD-1") is None
