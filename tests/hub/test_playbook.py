"""What the Curator's review runs remember of a project, on the hub: the playbook of the Curator, changed by deltas alone
(``evo_agents.hub.server.playbook``), what earlier nights found with what became of it (``evo_agents.hub.server.earlier``),
both in the prompt of the next review run, a closed finding not recorded again without new evidence, and evidence that
points at a line of a transcript. On test_history_runs' lab: the owner an admin with a charter, two sessions of theirs
with transcripts, and the night's review run.

Step 5 of plan history-driven-curator, criteria a4 (evidence of a line of a transcript, which the hub finds) and a11:
the Reviewer sees, for each lens of its night, the findings of earlier nights with their proposals and the ledger's
outcome, and the proposals the owner rejected lately; a closed finding is not recorded again without evidence it did
not cite; the playbook's entries have ids, evidence and counters of helpful and harmful, and change by deltas through
the worker's command, never written anew. Criterion a13: only additions to the contract."""

from __future__ import annotations

import json
from datetime import date

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_history_runs import FIRST, lab  # noqa: F401 (lab is a fixture)
from tests.hub.test_history_search import agent_call, held_review, text_of, two_sessions
from tests.hub.test_review_runs import REVIEW_COPY, answered, finding, proposed, second_review
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import OWNER, PROJECT, claim, moved

LINE = {"kind": "transcript", "session_id": FIRST, "line": 5}
PUSH = {"kind": "transcript", "session_id": FIRST, "line": 13}
NEXT = date(2026, 10, 11)  # a night whose lenses are tool_errors, environment and corrections


def delta(lab, run_id: int, **body):
    return lab.client.post(f"/v1/worker/runs/{run_id}/playbook", json=body, headers=lab.worker["headers"])


def written(lab, run_id: int, **body) -> dict:
    response = delta(lab, run_id, **body)
    assert response.status_code == 201, response.text
    return response.json()


def recorded(lab, run_id: int, **body) -> dict:
    response = finding(lab.client, lab.worker, run_id, **body)
    assert response.status_code == 201, response.text
    return response.json()


def playbook_of(lab, who: str = "owner", **params) -> dict:
    response = lab.client.get(f"/v1/projects/{PROJECT}/curator/playbook", params=params, headers=lab.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def earlier_of(lab, **params) -> dict:
    response = lab.client.get(f"/v1/projects/{PROJECT}/curator/earlier", params=params, headers=lab.headers["owner"])
    assert response.status_code == 200, response.text
    return response.json()


# Evidence of a line of a transcript


def test_evidence_of_a_line_of_a_transcript_is_found_on_the_hub_or_refused(lab):
    two_sessions(lab)
    run_id = held_review(lab)
    made = recorded(lab, run_id, lens="environment", title="the push is rejected", evidence=[LINE])
    assert made["evidence"] == [{**LINE, "file": "main", "resolved": "transcript"}]
    sub = {**LINE, "file": "subagents/agent-a1.jsonl"}
    for item, why in (
        ({**LINE, "line": 17}, f"transcript main of session {FIRST} has 16 lines, so no line 17"),
        (sub, f"project {PROJECT} has no transcript subagents/agent-a1.jsonl of session {FIRST}"),
        ({**LINE, "session_id": "0199b1c2-7d3e-7a10-9c41-00000000ffff"}, "has no transcript main of session"),
    ):
        refused = finding(lab.client, lab.worker, run_id, evidence=[item])
        assert refused.status_code == 422 and why in refused.json()["message"], refused.text
    bad = finding(lab.client, lab.worker, run_id, evidence=[{"kind": "transcript", "session_id": FIRST}])
    assert bad.status_code == 422 and "transcript evidence needs line" in bad.text
    deleted = lab.client.delete(f"/v1/projects/{PROJECT}/transcripts/{FIRST}", headers=lab.headers["owner"])
    assert deleted.status_code == 200, deleted.text
    gone = finding(lab.client, lab.worker, run_id, evidence=[LINE])
    assert gone.status_code == 422 and "was deleted" in gone.json()["message"]


# The playbook


def test_a_review_run_changes_the_playbook_by_deltas_alone(lab):
    two_sessions(lab)
    run_id = held_review(lab)
    entry = written(lab, run_id, op="add", lens="environment", text="Wait on   a file, never sleep.", evidence=[LINE])
    assert (entry["ref"], entry["lens"], entry["text"], entry["helpful"], entry["harmful"]) == (
        f"pb-{entry['id']}",
        "environment",
        "Wait on a file, never sleep.",
        0,
        0,
    )
    assert entry["evidence"] == [{**LINE, "file": "main", "resolved": "transcript"}]
    every = written(lab, run_id, op="add", text="Read the numbers before the transcript.", evidence=[PUSH])
    assert every["lens"] is None and every["created_run_id"] == run_id

    for body, code, why in (
        ({"op": "add", "text": "no evidence"}, 422, "at least one piece of evidence"),
        ({"op": "add", "text": "x", "evidence": [{**LINE, "line": 99}]}, 422, "has 16 lines"),
        ({"op": "helpful"}, 422, "takes the entry_id"),
        ({"op": "helpful", "entry_id": entry["id"], "text": "x"}, 422, "neither text nor lens"),
        ({"op": "retire", "entry_id": entry["id"]}, 422, "retire takes a note"),
        ({"op": "helpful", "entry_id": 999}, 404, "has no entry pb-999"),
    ):
        refused = delta(lab, run_id, **body)
        assert refused.status_code == code and why in refused.text, (body, refused.text)

    assert written(lab, run_id, op="helpful", entry_id=entry["id"], evidence=[PUSH])["helpful"] == 1
    again = delta(lab, run_id, op="helpful", entry_id=entry["id"])
    assert again.status_code == 409 and "once a run" in again.json()["message"]
    assert written(lab, run_id, op="harmful", entry_id=every["id"], note="it misled me")["harmful"] == 1
    shown = playbook_of(lab)
    assert [item["ref"] for item in shown["entries"]] == [entry["ref"], every["ref"]]  # the most helpful first
    assert (shown["live"], shown["retired"]) == (2, 0)
    assert [item["ref"] for item in playbook_of(lab, lens="cost")["entries"]] == [every["ref"]]

    retired = written(lab, run_id, op="retire", entry_id=every["id"], note="the figures cover it now")
    assert retired["retired"] is True and retired["retired_note"] == "the figures cover it now"
    late = delta(lab, run_id, op="helpful", entry_id=every["id"])
    assert late.status_code == 409 and "is retired" in late.json()["message"]
    assert [item["ref"] for item in playbook_of(lab)["entries"]] == [entry["ref"]]
    assert [item["ref"] for item in playbook_of(lab, retired="true")["entries"]] == [entry["ref"], every["ref"]]

    d = tables.curator_playbook_deltas
    ops = sql(lab.db, select(d.c.entry_id, d.c.op, d.c.run_id, d.c.note).order_by(d.c.id))
    assert ops == [
        (entry["id"], "add", run_id, None),
        (every["id"], "add", run_id, None),
        (entry["id"], "helpful", run_id, None),
        (every["id"], "harmful", run_id, "it misled me"),
        (every["id"], "retire", run_id, "the figures cover it now"),
    ]
    texts = [event["body"].get("text") for event in run_events(lab, run_id)]
    assert f"playbook entry {entry['ref']} counted helpful: Wait on a file, never sleep." in texts

    moved(lab.client, lab.worker, run_id, "verifying", "done")
    ended = delta(lab, run_id, op="helpful", entry_id=entry["id"])
    assert ended.status_code == 404  # only the agent of a review run its worker holds writes a delta
    assert (
        lab.client.get(f"/v1/projects/{PROJECT}/curator/playbook", headers=lab.headers["stranger"]).status_code == 404
    )


def run_events(lab, run_id: int) -> list[dict]:
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/events"
    return lab.client.get(path, params={"limit": 1000}, headers=lab.headers["owner"]).json()["events"]


# What earlier nights found


def first_night(lab) -> dict:
    """The first night's review: an environment finding with its proposal, which the owner rejects, another finding
    left open, and two entries of the playbook; the run ended. What it made, by name."""
    two_sessions(lab)
    run_id = held_review(lab)
    closed = recorded(lab, run_id, lens="environment", title="The push to origin is rejected", evidence=[LINE])
    left = recorded(lab, run_id, lens="corrections", title="The person asks for shorter docs", evidence=[PUSH])
    made = proposed(lab.client, lab.worker, run_id, lens="environment", finding_ids=[closed["id"]], evidence=[])
    entry = written(lab, run_id, op="add", lens="environment", text="Pull before a push.", evidence=[PUSH])
    written(lab, run_id, op="add", lens="cost", text="Count cache reads.", evidence=[LINE])
    moved(lab.client, lab.worker, run_id, "verifying", "done")
    answered(lab.client, lab.headers["owner"], made["id"], "reject", note="the remote is fine, not now")
    return {"run": run_id, "closed": closed, "left": left, "proposal": made, "entry": entry}


def next_review(lab, ended: int, when: date) -> dict:
    """A review run of the night ``when`` like the ended review run ``ended``, claimed and running: its claim, with
    the prompt the hub built for it."""
    r = tables.runs
    copied = (*REVIEW_COPY, "pinned_worker_id")
    (row,) = sql(lab.db, select(*(r.c[name] for name in copied)).where(r.c.id == ended))
    values = {**dict(zip(copied, row, strict=True)), "schedule_night": when}
    new_id = sql(lab.db, insert(r).values(**values).returning(r.c.id))[0][0]
    spec = claim(lab.client, lab.worker)
    assert spec["id"] == new_id
    moved(lab.client, lab.worker, new_id, "running")
    return spec


def test_the_next_review_prompt_holds_earlier_findings_rejected_proposals_and_the_playbook(lab):
    first = first_night(lab)
    spec = next_review(lab, first["run"], NEXT)
    text = spec["prompt"]
    closed, left, made = first["closed"], first["left"], first["proposal"]
    assert "# What earlier nights found through these lenses (curator_findings gives more)" in text
    assert (
        f"- environment #{closed['id']} (2026-10-08, medium, rejected: proposal #{made['id']} rejected): "
        "The push to origin is rejected"
    ) in text
    assert f"- corrections #{left['id']} (2026-10-08, medium, open): The person asks for shorter docs" in text
    assert "# Proposals the owner rejected in the last 30 days" in text
    assert f"- #{made['id']} (environment): {made['title']}; the owner's note: the remote is fine, not now" in text
    assert "# The playbook of the Curator for this project (curator_playbook gives it whole)" in text
    assert f"- {first['entry']['ref']} (environment, helpful 0, harmful 0): Pull before a push." in text
    assert "Count cache reads." not in text  # an entry of a lens that is not tonight's
    assert "history_search" in text and "transcript_lines" in text and "Read in two tiers" in text
    assert "`evo-agents worker playbook add --lens LENS --text TEXT --evidence SPEC`" in text
    assert "`transcript:ID:LINE` or `transcript:ID:FILE:LINE`" in text

    shown = earlier_of(lab, lens="environment")
    (item,) = shown["findings"]
    assert (item["id"], item["status"], item["closed"], item["night"]) == (closed["id"], "rejected", True, "2026-10-08")
    assert [(p["id"], p["state"], p["status"], p["note"]) for p in item["proposals"]] == [
        (made["id"], "rejected", "rejected", "the remote is fine, not now")
    ]
    assert item["evidence"] == [f"transcript:{FIRST}:5"]
    assert [p["id"] for p in shown["rejected"]] == [made["id"]]
    assert [f["id"] for f in earlier_of(lab, status="open")["findings"]] == [left["id"]]


def test_earlier_findings_show_only_the_proposals_their_reader_reads_by_label(lab):
    first = first_night(lab)
    p, made = tables.proposals, first["proposal"]
    (label,) = sql(lab.db, select(p.c.label).where(p.c.id == made["id"]))[0]
    sql(lab.db, update(p).values(label={**label, "level": "secret"}).where(p.c.id == made["id"]))  # above the hub sink
    shown = earlier_of(lab, lens="environment")
    (item,) = shown["findings"]
    assert (item["id"], item["proposals"], item["status"], item["closed"]) == (first["closed"]["id"], [], "open", False)
    assert shown["rejected"] == [] and "the remote is fine" not in json.dumps(shown)


def test_a_finding_like_a_closed_one_is_recorded_again_only_with_new_evidence(lab):
    first = first_night(lab)
    second = second_review(lab, first["run"], NEXT)
    closed = first["closed"]
    like = finding(lab.client, lab.worker, second, lens="environment", title="Push to origin rejected", evidence=[LINE])
    assert like.status_code == 409, like.text
    assert like.json()["message"].startswith(f"finding #{closed['id']} (environment, rejected) of an earlier night")
    same = finding(lab.client, lab.worker, second, lens="environment", title="Something else", evidence=[LINE])
    assert same.status_code == 409, "the very evidence of a closed finding, whatever the title"
    other_lens = finding(lab.client, lab.worker, second, lens="tool_errors", title="Push rejected", evidence=[LINE])
    assert other_lens.status_code == 201
    more = recorded(lab, second, lens="environment", title="The push to origin is rejected", evidence=[LINE, PUSH])
    assert len(more["evidence"]) == 2  # new evidence: recorded
    left = first["left"]
    repeat = finding(lab.client, lab.worker, second, lens="corrections", title=left["title"], evidence=[PUSH])
    assert repeat.status_code == 201, "an open finding is no closed one"

    text = text_of(agent_call(lab, second, "curator_findings", {"lens": "environment"}))
    assert f"#{closed['id']} environment (2026-10-08, medium, rejected, closed; proposals #" in text
    assert "Proposals the owner rejected lately:" in text and "note: the remote is fine, not now" in text
    shown = text_of(agent_call(lab, second, "curator_playbook", {}))
    assert shown.startswith("The playbook of project evo-agents: 2 live entries, 0 retired.")
    assert f"{first['entry']['ref']} (environment, helpful 0, harmful 0): Pull before a push. Evidence: " in shown


def test_the_command_line_reads_the_playbook_and_earlier_findings_with_the_json_keys_of_the_contract(
    lab, tmp_path, monkeypatch, capsys
):
    first = first_night(lab)
    with serving(create_app(lab.client.app.state.config)) as url:
        token = lab.headers["owner"]["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, OWNER, token)

        def run(*args: str):
            return cli(monkeypatch, capsys, home, "hub", "curator", *args, "--project", PROJECT)

        shown = as_json(run("playbook", "--json"))
        assert_json_keys("hub curator playbook", shown)
        assert [entry["text"] for entry in shown["entries"]] == ["Pull before a push.", "Count cache reads."]
        out = ok(run("playbook", "--lens", "cost")).out
        assert "Count cache reads." in out and "Pull before" not in out and "2 live entries, 0 retired" in out
        found = as_json(run("earlier", "--json"))
        assert_json_keys("hub curator earlier", found)
        assert {item["id"] for item in found["findings"]} == {first["closed"]["id"], first["left"]["id"]}
        out = ok(run("earlier", "--status", "rejected")).out
        assert f"#{first['closed']['id']}" in out and "rejected (closed)" in out
        assert f"rejected lately: #{first['proposal']['id']} environment fix" in out
