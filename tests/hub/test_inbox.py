"""``evo-agents hub inbox``: what waits on a person across hubs, read with no hub but a fake one (``evo_agents.hub.inbox``)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from evo_agents.hub import cli_inbox, inbox
from evo_agents.hub.client import HubError

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def ago(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def run(run_id: int, state: str, **fields) -> dict:
    return {
        "id": run_id,
        "project": "lumen",
        "kind": "plan",
        "state": state,
        "plan_id": "",
        "step_key": None,
        "dispatched_by": "ana",
        "queued_at": ago(600),
        **fields,
    }


def decision(decision_id: int, **fields) -> dict:
    return {
        "id": decision_id,
        "project": "lumen",
        "run_id": 7,
        "plan_id": "northwind-sync",
        "step_key": "3",
        "category": "scope",
        "question": "Which  region?",
        "options": [{"key": "eu", "label": "EU"}, {"key": "us", "label": "US", "recommended": True}],
        "recommended": None,
        "asked_of": "ana",
        "asked_at": ago(30),
        "credential_request": None,
        "state": "open",
        **fields,
    }


ANA = inbox.Login("https://hub.example", "ana", "t", None)
ANA_NORTH = inbox.Login("https://north.example", "ana", "t", "~/.evo/hub-north")


class FakeHub:
    """Answers GETs from a dict of path to body; a missing path is a 404."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[str] = []

    def call(self, method: str, path: str, body=None, **_):
        self.calls.append(path)
        key = path.split("?")[0]
        if key not in self.answers:
            raise HubError("not found", 404)
        answer = self.answers[key]
        return answer(parse_qs(urlsplit(path).query)) if callable(answer) else answer


def test_parse_ref_takes_ids_and_links():
    assert inbox.parse_ref("582") == inbox.Ref("decision", 582)
    assert inbox.parse_ref("d582") == inbox.Ref("decision", 582)
    assert inbox.parse_ref("decision:582") == inbox.Ref("decision", 582)
    assert inbox.parse_ref("r1092") == inbox.Ref("run", 1092)
    assert inbox.parse_ref("#1092") == inbox.Ref("run", 1092)
    assert inbox.parse_ref("https://hub.example/inbox?decision=12") == inbox.Ref("decision", 12, None, "hub.example")
    assert inbox.parse_ref("https://hub.example/p/lumen/runs/80#run-decision-32") == inbox.Ref(
        "decision", 32, "lumen", "hub.example"
    )
    assert inbox.parse_ref("https://hub.example/p/lumen/runs/948?view=log") == inbox.Ref(
        "run", 948, "lumen", "hub.example"
    )
    with pytest.raises(ValueError):
        inbox.parse_ref("https://hub.example/p/lumen/plans/x")
    with pytest.raises(ValueError):
        inbox.parse_ref("abc")


def test_decision_item_says_how_to_answer_and_where():
    item = inbox.decision_item(decision(5), ANA_NORTH, NOW)
    assert item["kind"] == "decision" and item["mine"] and item["age_min"] == 30
    assert item["title"] == "Which region?"
    assert item["next"] == "EVO_HUB_HOME=~/.evo/hub-north evo-agents hub decision answer 5 --project lumen --option us"
    asked = inbox.decision_item(
        decision(6, category="credential", credential_request={"env_var": "LUMEN_KEY"}), ANA, NOW
    )
    assert asked["kind"] == "credential"
    assert asked["next"].endswith("decision answer 6 --project lumen --secret  # LUMEN_KEY from stdin")
    other = inbox.decision_item(decision(7, asked_of="bo"), ANA, NOW)
    assert not other["mine"]


def test_run_items_keep_what_needs_a_person():
    runs = [
        run(1, "review"),
        run(2, "running", last_activity_at=ago(45), worker="w1"),
        run(3, "running", last_activity_at=ago(5)),
        run(4, "failed", failure_cause="credentials", error="git cannot read", finished_at=ago(60)),
        run(5, "failed", failure_cause="timeout", finished_at=ago(60 * 100)),  # ended before --since
        run(6, "lost", plan_id="northwind-sync", finished_at=ago(60)),
        run(8, "queued", plan_id="northwind-sync"),  # the plan runs again
        run(9, "waiting", waiting_since=ago(20)),  # on decision 50, which is its item
        run(10, "running", kind="local", last_activity_at=ago(500)),  # a person's own session
        run(11, "parked", parked_at=ago(15)),
    ]
    items = inbox.run_items(runs, {9}, ANA, NOW, stale=30, since=72)
    assert {item["id"]: item["kind"] for item in items} == {1: "review", 2: "stale", 4: "failed", 11: "parked"}
    by_id = {item["id"]: item for item in items}
    assert by_id[4]["title"] == "credentials: git cannot read"
    assert by_id[1]["next"] == "evo-agents hub run approve 1 --project lumen"
    assert by_id[4]["next"] == "evo-agents hub run candidates 4 --project lumen"
    assert by_id[2]["title"] == "running, no event for 45 min"


def test_a_rerun_or_resume_hides_the_failed_run():
    failed = run(4, "failed", finished_at=ago(10))
    assert inbox.ran_again(failed, [failed, run(12, "done", parent_run_id=4)])
    assert inbox.ran_again(failed, [failed, run(12, "running", resume_of_run_id=4)])
    assert not inbox.ran_again(failed, [failed, run(3, "done", parent_run_id=4)])  # older
    step = run(4, "failed", plan_id="p", step_key="2")
    assert not inbox.ran_again(step, [step, run(12, "done", plan_id="p", step_key="5")])
    assert inbox.ran_again(step, [step, run(12, "done", plan_id="p", step_key="2")])


def test_notices_that_only_report_are_counted():
    notes = [
        {"id": 1, "kind": "notice", "notice_kind": "merge_default_branch", "title": "PR merged", "project": "lumen"},
        {
            "id": 2,
            "kind": "notice",
            "notice_kind": "chat_mention",
            "title": "@ana",
            "project": "lumen",
            "link": "/chats/3",
        },
        {"id": 3, "kind": "decision", "notice_kind": None, "title": "asks", "project": "lumen"},
        {"id": 4, "kind": "proposal", "notice_kind": None, "proposal_state": "open", "title": "p", "project": "lumen"},
        {
            "id": 5,
            "kind": "proposal",
            "notice_kind": None,
            "proposal_state": "applied",
            "title": "q",
            "project": "lumen",
        },
        {"id": 6, "kind": "notice", "notice_kind": "run_outputs", "title": "files", "project": "lumen"},
    ]
    items, reports = inbox.notice_items(notes, ANA, NOW)
    assert [(item["kind"], item["id"]) for item in items] == [("notice", 2), ("proposal", 4)]
    assert reports == 2
    assert items[0]["link"] == "https://hub.example/chats/3"


def test_failed_runs_with_one_cause_fold():
    items = [
        {"kind": "failed", "id": n, "project": "lumen", "category": "chat", "asked_of": "bo", "title": "credentials: x"}
        for n in (3, 5, 4)
    ] + [{"kind": "review", "id": 9, "project": "lumen", "category": "plan", "asked_of": "ana", "title": "r"}]
    folded = inbox.fold_same_cause(items)
    assert [(item["id"], item.get("also")) for item in folded] == [(9, None), (5, [4, 3])]


def test_gather_one_reads_every_project_and_orders_mine_first(monkeypatch):
    answers = {
        "/v1/projects": [{"name": "lumen", "role": "writer"}, {"name": "northwind", "role": None}],
        "/v1/me/notifications": {"notifications": []},
        "/v1/projects/lumen/decisions": {
            "decisions": [
                decision(50, run_id=9),
                decision(51, asked_of="bo", run_id=None),
                decision(52, category="wait", run_id=13),
            ]
        },
        "/v1/projects/lumen/runs": {"runs": [run(9, "waiting"), run(1, "review"), run(13, "parked")]},
    }
    hub = FakeHub(answers)
    monkeypatch.setattr(inbox.Login, "hub", lambda self: hub)
    got = inbox.gather_one(ANA, now=NOW)
    assert got.error is None
    assert [(item["kind"], item["id"]) for item in got.items] == [("decision", 50), ("review", 1), ("decision", 51)]
    assert not any("northwind" in call for call in hub.calls)  # no role, not read
    with_waits = inbox.gather_one(ANA, now=NOW, waits=True)
    assert 52 in [item["id"] for item in with_waits.items]


def test_gather_one_reports_a_hub_it_cannot_read(monkeypatch):
    monkeypatch.setattr(inbox.Login, "hub", lambda self: FakeHub({}))
    got = inbox.gather_one(ANA, now=NOW)
    assert got.error == "not found" and got.items == []


def test_logins_read_every_login_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("EVO_HUB_HOME", raising=False)
    monkeypatch.delenv("EVO_HUB_TOKEN", raising=False)
    monkeypatch.delenv("EVO_HUB_URL", raising=False)
    for name, url in (
        ("hub", "https://hub.example"),
        ("hub-north", "https://north.example"),
        ("hub-old", "https://hub.example"),
    ):
        directory = tmp_path / ".evo" / name
        directory.mkdir(parents=True)
        (directory / "config.json").write_text(json.dumps({"url": url, "login": "ana"}))
        (directory / "token").write_text("tok\n")
    found = inbox.logins()
    assert [(login.url, login.home) for login in found] == [
        ("https://hub.example", None),
        ("https://north.example", "~/.evo/hub-north"),
    ]
    assert [login.url for login in inbox.logins("north")] == ["https://north.example"]
    monkeypatch.setenv("EVO_HUB_HOME", str(tmp_path / ".evo" / "hub-north"))
    assert [(login.url, login.home) for login in inbox.logins()] == [
        ("https://north.example", None),
        ("https://hub.example", "~/.evo/hub"),
    ]


def test_detail_finds_a_decision_in_another_project(monkeypatch):
    answers = {
        "/v1/projects": [{"name": "lumen", "role": "writer"}, {"name": "northwind", "role": "reader"}],
        "/v1/projects/northwind/decisions/40": decision(40, project="northwind", run_id=77),
        "/v1/projects/northwind/runs/77": run(77, "waiting", project="northwind", last_seq=30, worker="w1"),
        "/v1/projects/northwind/runs/77/credentials": [{"name": "LUMEN_KEY", "revoked_at": None}],
        "/v1/projects/northwind/runs/77/events": {
            "events": [{"seq": 29, "at": ago(3), "kind": "agent_message_chunk", "body": {"text": "asked"}}]
        },
    }
    hub = FakeHub(answers)
    monkeypatch.setattr(inbox.Login, "hub", lambda self: hub)
    found = inbox.detail(inbox.Ref("decision", 40), [ANA], first="lumen")
    assert found["project"] == "northwind"
    assert found["run"]["last_activity_at"] == ago(3)
    assert found["next"] == ["evo-agents hub decision answer 40 --project northwind --option us"]
    assert found["diagnosis"].startswith("waiting on decision #40 (scope)")
    assert hub.calls[1].startswith("/v1/projects/lumen/decisions/40")  # the harness's project first
    lines = cli_inbox.show_lines(found)
    assert "Leases: LUMEN_KEY" in lines and lines[-1].startswith("-> evo-agents hub decision answer 40")
    assert inbox.detail(inbox.Ref("decision", 41), [ANA]) is None
    assert inbox.detail(inbox.Ref("run", 77, host="other.example"), [ANA]) is None


def test_inbox_lines_print_each_item_with_its_command():
    hub = inbox.HubInbox("https://north.example", "ana", reports=3)
    hub.items = [inbox.decision_item(decision(5), ANA_NORTH, NOW)]
    lines = cli_inbox.inbox_lines([hub, inbox.HubInbox("https://down.example", "ana", error="cannot reach")])
    assert lines[0] == "https://north.example (ana): 1 decision; 3 unread notice(s) only report"
    assert lines[1].split() == ["DECISION", "#5", "30m", "lumen,", "run", "#7,", "northwind-sync", "step", "3"]
    assert lines[3].strip().startswith("-> EVO_HUB_HOME=~/.evo/hub-north evo-agents hub decision answer 5")
    assert lines[5] == "https://down.example: cannot read it: cannot reach"
