"""The probes of the Curator (plan correction-recurrence, step 1): a task a session got wrong, from a base commit, and
the one-line verify that exits 0 when an agent given it does not repeat the mistake.

A proposal whose evidence cites a correction of the night's figures and that changes a file a probe arm can place
carries one, or the hub answers 422; accepting it attaches the probe to the change; a review run adds one to a merged
change that has none; admins add, edit and retire them, and they alone read the verify. The charter's ``probes`` key
says how many arms, on which models, within which weekly budget. Every name here is made up.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from evo_agents.hub import probes
from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.probe_cli import PROBE_KEYS
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.curator import CharterBody
from evo_agents.hub.server.probes import PROBE as ACTION
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import PROJECT, THE_NIGHT, charter_body
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    accepted,
    one_change,
    path,
    proposal_of,
    review_run,
    world,
)
from tests.hub.test_review_runs import DRAFT, proposal, second_review
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import moved

AGENTS = [{"repo": "evo-agents", "path": "AGENTS.md"}]
PROBE = {
    "repo": "evo-agents",
    "commit": "abc1234",
    "task": "Wait for the fake build of widget-lab to finish, then read its log.",
    "verify": "python -m pytest -q tests/test_wait.py",
}
FAKE_SECRET = "FAKE_WIDGET_TOKEN=notarealvalue123"


# The shape of a probe


@pytest.mark.parametrize(
    ("where", "placed"),
    [
        ("AGENTS.md", True),
        ("docs/CLAUDE.md", True),
        (".claude/settings.json", True),
        (".claude/hooks/pre_tool.sh", True),
        (".claude/hooks", False),
        ("src/.claude/settings.json", False),
        ("evo_agents/worker/wait.py", False),
        ("../AGENTS.md", False),
        ("", False),
    ],
)
def test_probe_path_names_the_files_a_probe_arm_can_place(where, placed):
    assert probes.probe_path(where) is placed


def test_probe_paths_are_each_once_and_sorted():
    assert probes.probe_paths(["src/a.py", "CLAUDE.md", "AGENTS.md", "AGENTS.md"]) == ["AGENTS.md", "CLAUDE.md"]


@pytest.mark.parametrize(
    ("verify", "why"),
    [
        ("", "is empty"),
        ("pytest -q\nexit 0", "more than one line"),
        ("pytest " + "x" * probes.MAX_VERIFY_CHARS, "more than 2000"),
        ("true", "runs no program"),
    ],
)
def test_a_probe_verify_is_one_line_that_runs_a_real_command(verify, why):
    assert why in probes.verify_problem(verify)


def test_a_probe_verify_of_a_real_command_passes():
    assert probes.verify_problem("python -m pytest -q tests/test_wait.py") is None


# The charter's probes key


def test_charter_probes_default_to_three_arms_on_the_builder_and_no_budget():
    body = CharterBody.model_validate(charter_body(builder={"runtime": "codex", "model": "gpt-fake-1"}))
    assert body.probes.k == probes.DEFAULT_K and body.probes.budget_usd is None
    assert [model.model_dump() for model in body.probes.models] == [{"runtime": "codex", "model": "gpt-fake-1"}]


def test_charter_probes_keep_the_models_they_name():
    models = [{"runtime": "claude-code", "model": None}, {"runtime": "codex", "model": "gpt-fake-1"}]
    body = CharterBody.model_validate(charter_body(probes={"k": 10, "models": models, "budget_usd": 12.5}))
    assert (body.probes.k, body.probes.budget_usd) == (10, 12.5)
    assert [model.model_dump() for model in body.probes.models] == models


@pytest.mark.parametrize(
    "bad",
    [
        {"k": 0},
        {"k": 11},
        {"models": [{"runtime": "codex", "model": "m"}, {"runtime": "codex", "model": "m"}]},
        {"models": [{"runtime": "no-such-runtime"}]},
        {"budget_usd": 0},
        {"budget_usd": probes.MAX_WEEK_BUDGET_USD + 1},
        {"arms": 3},
    ],
)
def test_charter_probes_refuse_invalid_values(bad):
    with pytest.raises(ValidationError):
        CharterBody.model_validate(charter_body(probes=bad))


def test_charter_probes_show_in_the_charter_the_hub_answers(world):
    response = world.client.get(path("charter"), headers=world.headers["owner"])
    assert response.status_code == 200, response.text
    assert response.json()["probes"] == {
        "k": 3,
        "models": [{"runtime": "claude-code", "model": None}],
        "budget_usd": None,
    }
    refused = world.client.put(
        path("charter"), json={**response.json(), "probes": {"k": 11}}, headers=world.headers["owner"]
    )
    assert refused.status_code == 422, refused.text


# A probe on a proposal


def corrections_cited(world, run_id: int) -> None:
    """The night's figures of review run ``run_id`` with a correction its run evidence cites."""
    cf = tables.curator_figures
    ((figures,),) = sql(world.db, select(cf.c.figures).where(cf.c.run_id == run_id))
    entry = {"session": "s-fake", "text": "no, wait for the build", "evidence": f"run:{run_id}:1"}
    sql(world.db, update(cf).where(cf.c.run_id == run_id).values(figures={**figures, "corrections": [entry]}))


def propose(world, run_id: int, **body):
    sent = {"kind": "docs", "paths": AGENTS, "evidence": [{"kind": "run", "run_id": run_id, "seq": 1}], **body}
    return proposal(world.client, world.worker, run_id, plan=DRAFT, **sent)


def test_a_proposal_citing_a_correction_on_agents_md_needs_a_probe(world):
    run_id = review_run(world)
    corrections_cited(world, run_id)
    refused = propose(world, run_id)
    assert refused.status_code == 422, refused.text
    assert "give it a probe" in refused.text and "nothing was recorded" in refused.text
    assert sql(world.db, select(tables.proposals.c.id)) == []

    other = propose(world, run_id, paths=[{"repo": "evo-agents", "path": "evo_agents/worker/wait.py"}], kind="fix")
    assert other.status_code == 201, other.text  # no file a probe arm can place: no probe needed


def test_a_proposal_without_cited_corrections_needs_no_probe(world):
    run_id = review_run(world)
    made = propose(world, run_id)
    assert made.status_code == 201, made.text
    assert made.json()["probe"] is None


def test_a_proposal_probe_is_masked_hidden_from_the_agent_and_attached_on_accept(world):
    run_id = review_run(world)
    corrections_cited(world, run_id)
    sent = {**PROBE, "task": f"Use {FAKE_SECRET} to read the log.", "verify": f"{PROBE['verify']} # {FAKE_SECRET}"}
    made = propose(world, run_id, probe=sent)
    assert made.status_code == 201, made.text
    probe = made.json()["probe"]
    assert probe["verify"] is None, "the agent of a run does not read the answer key"
    assert "notarealvalue123" not in probe["task"] and "***" in probe["task"]
    assert (probe["source"], probe["change_id"], probe["commit"]) == ("proposal", None, "abc1234")
    pr = tables.curator_probes
    ((stored,),) = sql(world.db, select(pr.c.verify))
    assert "notarealvalue123" not in stored and stored.startswith(PROBE["verify"])

    moved(world.client, world.worker, run_id, "verifying", "done")
    answer = world.client.post(
        path("proposals", str(made.json()["id"]), "answer"), json={"action": "accept"}, headers=world.headers["owner"]
    )
    assert answer.status_code == 200, answer.text
    change = one_change(world)
    shown = proposal_of(world, made.json()["id"])["probe"]
    assert shown["change_id"] == change["id"] and shown["verify"] == stored  # owner is an admin

    listed = world.client.get(path("probes"), params={"change": change["id"]}, headers=world.headers["other"])
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1 and listed.json()["probes"][0]["verify"] is None  # other is a writer


def test_a_proposal_probe_of_a_repo_it_does_not_change_is_refused(world):
    run_id = review_run(world)
    refused = propose(world, run_id, probe={**PROBE, "repo": "lumen-kb-docs"})
    assert refused.status_code == 422, refused.text
    assert "none of the repos the proposal changes" in refused.text


@pytest.mark.parametrize("verify", ["true", "pytest -q\nexit 0", ""])
def test_a_proposal_probe_with_a_bad_verify_is_refused(world, verify):
    run_id = review_run(world)
    refused = propose(world, run_id, probe={**PROBE, "verify": verify})
    assert refused.status_code == 422, refused.text


# Probes the admins write


def test_admins_add_edit_and_retire_a_probe_and_writers_are_refused(world):
    accepted(world)
    change = one_change(world)
    owner, other = world.headers["owner"], world.headers["other"]
    sent = {**PROBE, "change_id": change["id"]}
    assert world.client.post(path("probes"), json=sent, headers=other).status_code == 403

    added = world.client.post(path("probes"), json=sent, headers=owner)
    assert added.status_code == 201, added.text
    probe = added.json()
    assert (probe["source"], probe["change_id"], probe["verify"]) == ("admin", change["id"], PROBE["verify"])
    again = world.client.post(path("probes"), json=sent, headers=owner)
    assert again.status_code == 409, again.text

    one = path("probes", str(probe["id"]))
    assert world.client.get(one, headers=other).json()["verify"] is None
    assert world.client.patch(one, json={"task": "Another task."}, headers=other).status_code == 403
    edited = world.client.patch(one, json={"task": "Another task.", "commit": None}, headers=owner)
    assert edited.status_code == 200, edited.text
    assert (edited.json()["task"], edited.json()["commit"]) == ("Another task.", None)
    assert world.client.patch(one, json={}, headers=owner).status_code == 422

    assert world.client.post(f"{one}/retire", json={}, headers=other).status_code == 403
    retired = world.client.post(f"{one}/retire", json={"note": "a fake reason"}, headers=owner)
    assert retired.status_code == 200, retired.text
    assert (retired.json()["state"], retired.json()["retired_note"]) == ("retired", "a fake reason")
    assert world.client.post(f"{one}/retire", json={}, headers=owner).status_code == 409
    assert world.client.patch(one, json={"task": "x"}, headers=owner).status_code == 409

    a = tables.audit
    targets = [row[0] for row in sql(world.db, select(a.c.target).where(a.c.action == ACTION))]
    assert [target.split()[2] for target in targets] == ["add", "edit", "retire"]


def test_an_admin_probe_needs_one_known_target_and_repo(world):
    accepted(world)
    change = one_change(world)
    owner = world.headers["owner"]
    both = {**PROBE, "change_id": change["id"], "proposal_id": change["proposal_id"]}
    assert world.client.post(path("probes"), json=both, headers=owner).status_code == 422
    assert world.client.post(path("probes"), json={**PROBE, "change_id": 9999}, headers=owner).status_code == 404
    unknown = {**PROBE, "repo": "no-such-repo", "change_id": change["id"]}
    assert world.client.post(path("probes"), json=unknown, headers=owner).status_code == 422


# Probes a review run adds


def test_a_review_run_adds_a_probe_to_a_merged_change_only(world):
    first = accepted(world)
    change = one_change(world)
    run_id = second_review(world, first["run_id"], THE_NIGHT + timedelta(days=1))
    sent = {**PROBE, "change_id": change["id"]}
    added = f"/v1/worker/runs/{run_id}/probes"
    early = world.client.post(added, json=sent, headers=world.worker["headers"])
    assert early.status_code == 409 and "merged change alone" in early.text, early.text

    c = tables.curator_changes
    sql(world.db, update(c).where(c.c.id == change["id"]).values(state="merged", merged_at=func.now()))
    made = world.client.post(added, json=sent, headers=world.worker["headers"])
    assert made.status_code == 201, made.text
    assert (made.json()["source"], made.json()["created_run_id"], made.json()["verify"]) == ("review", run_id, None)
    again = world.client.post(added, json=sent, headers=world.worker["headers"])
    assert again.status_code == 409 and "already" in again.text, again.text


# The command line


def test_curator_probe_list_show_add_and_retire_on_the_command_line(world, tmp_path, monkeypatch, capsys):
    accepted(world)
    change = one_change(world)
    with serving(create_app(world.app.state.config)) as url:
        homes = {
            who: write_credentials(
                tmp_path / who, url, who, world.headers[who]["Authorization"].removeprefix("Bearer ")
            )
            for who in ("owner", "other")
        }

        def curator(who: str, *args: str):
            return cli(monkeypatch, capsys, homes[who], "hub", "curator", "probe", *args, "--project", PROJECT)

        task = tmp_path / "task.md"
        task.write_text("Wait for the fake build, then read its log.\n", encoding="utf-8")
        added = as_json(
            curator(
                "owner",
                "add",
                "--change",
                str(change["id"]),
                "--repo",
                "evo-agents",
                "--task-file",
                str(task),
                "--verify",
                PROBE["verify"],
                "--json",
            )
        )
        assert_json_keys("hub curator probe add", added)
        assert set(added) == set(PROBE_KEYS) and added["verify"] == PROBE["verify"]

        listed = as_json(curator("other", "list", "--change", str(change["id"]), "--json"))
        assert_json_keys("hub curator probe list", listed)
        assert listed["total"] == 1 and listed["probes"][0]["verify"] is None
        assert "1 probe(s) in" in ok(curator("other", "list")).out

        shown = as_json(curator("owner", "show", str(added["id"]), "--json"))
        assert_json_keys("hub curator probe show", shown)
        assert shown["verify"] == PROBE["verify"]
        assert "(hidden" in ok(curator("other", "show", str(added["id"]))).out

        refused = curator("other", "retire", str(added["id"]))
        assert refused.code != 0 and "admin" in refused.err
        retired = as_json(curator("owner", "retire", str(added["id"]), "--note", "a fake reason", "--json"))
        assert_json_keys("hub curator probe retire", retired)
        assert retired["state"] == "retired"
