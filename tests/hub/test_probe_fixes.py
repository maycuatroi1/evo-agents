"""What the weekly probe report asks of the next review run (plan correction-recurrence, step 4, criterion a5): a probe
recurring on one model two weeks in a row asks for a fix proposal of its change's rule (at least tier 1 by the hub's
code, the two weeks' probe results as its evidence, one open per change) or a finding saying why not; a probe not
reproduced two weeks in a row asks for a probe in its place. The report names both. Every name here is made up.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from evo_agents.hub import probes, review, tiers
from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import written
from tests.hub.test_curator_changes import curator_charter, world  # noqa: F401 (world is a fixture)
from tests.hub.test_probe_report import asked
from tests.hub.test_probe_runs import OPUS, SONNET, WEEK, probing
from tests.hub.test_probes import PROBE
from tests.hub.test_review_runs import REVIEW_COPY, finding, proposal
from tests.hub.test_runs import claim, moved

NEXT = WEEK + timedelta(days=14)  # the week of the review run that reads the reports of WEEK and the week after
NIGHT = NEXT + timedelta(days=3)  # a Thursday
SESSIONS = {  # (arm, index, passed) of each session of a done probe run of k=2
    "recurring": [("on", 1, False), ("on", 2, True), ("off", 1, False), ("off", 2, False)],
    "not_reproduced": [("on", 1, True), ("on", 2, True), ("off", 1, True), ("off", 2, True)],
    "holds": [("on", 1, True), ("on", 2, True), ("off", 1, False), ("off", 2, True)],
}


def weekly(world, probe: dict, statuses: tuple[str, str], *, model: str = "opus") -> None:
    """Rows of the probe runs of WEEK and the week after on ``model``, done, with the two ``statuses``."""
    pw = tables.curator_probe_runs
    (project_id,) = sql(
        world.db, select(tables.curator_probes.c.project_id).where(tables.curator_probes.c.id == probe["id"])
    )[0]
    for week, status in zip((WEEK, WEEK + timedelta(days=7)), statuses, strict=True):
        sessions = [{"arm": arm, "index": index, "passed": passed} for arm, index, passed in SESSIONS[status]]
        row = {"project_id": project_id, "probe_id": probe["id"], "change_id": probe["change_id"], "week": week}
        row |= {"runtime": "claude-code", "model": model, "k": 2, "state": "done", "sessions": sessions}
        sql(world.db, insert(pw).values(row))


def next_review(world, night: date = NIGHT) -> dict:
    """A review run of ``night`` like the ended one, queued directly and claimed: its spec, running."""
    r = tables.runs
    (ended,) = sql(world.db, select(r.c.id).where(r.c.kind == "review").order_by(r.c.id.desc()).limit(1))[0]
    (row,) = sql(world.db, select(*(r.c[name] for name in (*REVIEW_COPY, "pinned_worker_id"))).where(r.c.id == ended))
    values = {**dict(zip((*REVIEW_COPY, "pinned_worker_id"), row, strict=True)), "schedule_night": night}
    new_id = sql(world.db, insert(r).values(**values).returning(r.c.id))[0][0]
    spec = claim(world.client, world.worker)
    assert spec["id"] == new_id
    moved(world.client, world.worker, new_id, "running")
    return spec


def fix(world, run_id: int, change_id: int, **body):
    sent = {"lens": "corrections", "kind": "docs", "title": "Turn the waiting rule into a hook", "evidence": []}
    sent |= {"paths": [{"repo": "evo-agents", "path": "AGENTS.md"}], "fix_of": change_id, **body}
    return proposal(world.client, world.worker, run_id, **sent)


def test_probe_statuses_repeated_in_every_week_by_key():
    older = {
        (1, "claude-code", "opus"): "recurring",
        (1, "claude-code", "sonnet"): "recurring",
        (2, "x", None): "holds",
    }
    newer = {(1, "claude-code", "opus"): "recurring", (1, "claude-code", "sonnet"): "holds", (2, "x", None): "holds"}
    assert probes.repeated([older, newer], "recurring") == [(1, "claude-code", "opus")]
    assert probes.repeated([older, newer], "holds") == [(2, "x", None)]
    assert probes.repeated([], "recurring") == []


def test_a_fix_proposal_is_at_least_tier_1_and_never_tier_0_by_its_replays():
    docs = tiers.tier_of("docs", [("evo-agents", "AGENTS.md")], [])
    assert docs.tier == 0
    fixing = tiers.tier_of("docs", [("evo-agents", "AGENTS.md")], [], fix_of=7)
    assert fixing.tier == tiers.FIX_TIER == 1 and "change #7" in fixing.reasons[-1]
    assert tiers.tier_of("feature", [("evo-agents", "a/migrations/x.py")], [], fix_of=7).tier == 2
    won = {"harness": "AGENTS.md", "won": "3 of 3 holdout tasks"}
    assert tiers.replay_tier("docs", tiers.Tier(1), **won).tier == 0
    assert tiers.replay_tier("docs", tiers.Tier(1), **won, fix=True).tier == 1


def test_probe_evidence_reads_as_the_command_line_would_write_it():
    item = {"kind": "probe", "probe_id": 4, "week": "2026-10-05", "runtime": "claude-code", "model": "opus"}
    assert review.evidence_text(item) == "probe:4:2026-10-05:claude-code:opus"
    assert review.evidence_text({**item, "model": None}) == "probe:4:2026-10-05:claude-code"


def test_a_probe_recurring_two_weeks_on_one_model_asks_the_next_review_run_for_a_fix_proposal(world):
    probe = probing(world, models=(OPUS, SONNET), k=2)
    change_id = probe["change_id"]
    weekly(world, probe, ("recurring", "recurring"))
    weekly(world, probe, ("recurring", "holds"), model="sonnet")  # once on sonnet is not twice
    spec = next_review(world)
    prompt = spec["prompt"]
    assert "# Probe assignments of the weekly report" in prompt
    assert f"Fix change #{change_id}" in prompt and f"--fix-of {change_id}" in prompt
    assert f"probe #{probe['id']} on claude-code opus recurred" in prompt and "sonnet recurred" not in prompt
    assert "hook" in prompt and f"evo-agents worker finding --fix-of {change_id}" in prompt
    assert PROBE["verify"] not in prompt
    # The hub refuses a fix of a change it did not ask to fix.
    refused = fix(world, spec["id"], change_id + 100)
    assert refused.status_code == 422 and "fix_of" in refused.text
    made = fix(world, spec["id"], change_id)
    assert made.status_code == 201, made.text
    found = made.json()
    assert (found["fix_of"], found["tier"], found["state"]) == (change_id, 1, "open")
    assert f"it fixes the rule of change #{change_id}" in found["tier_reasons"][-1]
    shown = [(item["kind"], item["week"], item["model"], item["status"]) for item in found["evidence"]]
    assert shown == [
        ("probe", WEEK.isoformat(), "opus", "recurring"),
        ("probe", (WEEK + timedelta(days=7)).isoformat(), "opus", "recurring"),
    ]
    assert found["evidence"][0]["on"] == {"runs": 2, "failed": 1} and found["evidence_count"] == 2
    # One open fix proposal per change.
    again = fix(world, spec["id"], change_id, title="Add a check of the waiting rule")
    assert again.status_code == 409 and f"#{found['id']}" in again.text
    # A finding may still say why some other route is no fix; the hub adds the probe results to it.
    why = finding(
        world.client,
        world.worker,
        spec["id"],
        lens="corrections",
        title="No hook can see the wait",
        evidence=[{"kind": "code", "repo": "evo-agents", "path": "AGENTS.md"}],
        fix_of=change_id,
    )
    assert why.status_code == 201, why.text
    assert why.json()["fix_of"] == change_id and [item["kind"] for item in why.json()["evidence"]] == [
        "code",
        "probe",
        "probe",
    ]
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    # The next review run of the week is not asked again; the report names the fix proposal.
    later = next_review(world, NIGHT + timedelta(days=1))
    assert f"Fix change #{change_id}" not in later["prompt"]
    report = asked(world, weeks=1, week=NEXT.isoformat())
    assert report["fixes"] == [
        {"proposal_id": found["id"], "change_id": change_id, "title": found["title"], "state": "open"}
    ]
    # The hub reverts nothing on what the probe found, and never merges the fix alone.
    p = tables.proposals
    assert sql(world.db, select(func.count()).where(p.c.kind == "revert")) == [(0,)]
    assert sql(world.db, select(p.c.tier).where(p.c.id == found["id"])) == [(1,)]


def test_a_fix_proposal_without_evidence_of_its_own_needs_fix_of(world):
    probe = probing(world, k=2)
    spec = next_review(world)
    plain = fix(world, spec["id"], probe["change_id"], fix_of=None)
    assert plain.status_code == 422
    asked_none = fix(world, spec["id"], probe["change_id"])
    assert asked_none.status_code == 422  # no probe recurred: nothing was asked


def test_a_tier_2_fix_proposal_waits_for_room_in_max_decisions_per_day(world):
    probe = probing(world, k=2)
    written(
        world.client,
        world.headers["owner"],
        curator_charter(probes={"k": 2, "budget_usd": 3.0, "models": [OPUS]}, max_decisions_per_day=1),
    )
    p = tables.proposals
    sql(world.db, update(p).values(inbox_at=func.now()))  # today's one decision is used
    weekly(world, probe, ("recurring", "recurring"))
    spec = next_review(world)
    made = fix(world, spec["id"], probe["change_id"], paths=[{"repo": "evo-agents", "path": "app/migrations/x.py"}])
    assert made.status_code == 201, made.text
    assert made.json()["tier"] == 2
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    assert sql(world.db, select(p.c.inbox_at).where(p.c.id == made.json()["id"])) == [(None,)]


def test_a_probe_not_reproduced_two_weeks_asks_for_a_probe_in_its_place(world):
    probe = probing(world, k=2)
    change_id = probe["change_id"]
    weekly(world, probe, ("not_reproduced", "not_reproduced"))
    spec = next_review(world)
    prompt = spec["prompt"]
    assert f"Replace probe #{probe['id']} of change #{change_id}" in prompt
    assert f"--replaces {probe['id']}" in prompt and PROBE["task"] in prompt and PROBE["verify"] not in prompt
    url = f"/v1/worker/runs/{spec['id']}/probes"
    sent = {**PROBE, "task": "Wait for the fake build without a helper, then read its log.", "change_id": change_id}
    plain = world.client.post(url, json=sent, headers=world.worker["headers"])
    assert plain.status_code == 409  # the change has its probe: only a replacement takes its place
    wrong = world.client.post(url, json={**sent, "replaces": probe["id"] + 100}, headers=world.worker["headers"])
    assert wrong.status_code == 422
    made = world.client.post(url, json={**sent, "replaces": probe["id"]}, headers=world.worker["headers"])
    assert made.status_code == 201, made.text
    assert made.json()["replaces"] == probe["id"] and made.json()["state"] == "active"
    pr = tables.curator_probes
    ((state, note),) = sql(world.db, select(pr.c.state, pr.c.retired_note).where(pr.c.id == probe["id"]))
    assert state == "retired" and f"review run #{spec['id']}" in note
    report = asked(world, weeks=1, week=NEXT.isoformat())
    assert [(item["probe_id"], item["replaces"]) for item in report["replacements"]] == [
        (made.json()["id"], probe["id"])
    ]
    # Asked once: the old probe is retired, so the next review run is not asked again.
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    assert "Replace probe" not in next_review(world, NIGHT + timedelta(days=1))["prompt"]


def test_a_probe_that_holds_asks_nothing(world):
    probe = probing(world, k=2)
    weekly(world, probe, ("holds", "recurring"))
    assert "# Probe assignments" not in next_review(world)["prompt"]
    assert asked(world, weeks=1, week=NEXT.isoformat())["fixes"] == []
