"""What `evo-agents worker wait` and `worker ask --wait-option` read and send (``agent_input``, ``worker.cli``)."""

import asyncio
import json
import types
from datetime import UTC, datetime

import pytest

import evo_agents.worker.cli as worker_cli
from evo_agents.worker.agent_input import payload_contains, wait_until
from evo_agents.worker.home import WorkerHome, WorkerStateError

NOW = datetime(2026, 10, 24, 15, 54, tzinfo=UTC)
RUN = 707


def test_until_takes_a_time_with_a_timezone_or_a_span_from_now():
    assert wait_until("2026-10-30T09:00Z", NOW) == "2026-10-30T09:00:00+00:00"
    assert wait_until("2026-10-30T16:00+07:00", NOW) == "2026-10-30T09:00:00+00:00"
    assert wait_until("+6h", NOW) == "2026-10-24T21:54:00+00:00"
    assert wait_until(" +90m ", NOW) == "2026-10-24T17:24:00+00:00"
    assert wait_until("+5d", NOW) == "2026-10-29T15:54:00+00:00"
    for bad in ("2026-10-30T09:00", "tomorrow", "+6w", "6h", ""):
        with pytest.raises(WorkerStateError, match="--until takes"):
            wait_until(bad, NOW)


def test_contains_reads_each_value_as_json_when_it_is():
    assert payload_contains(["canvas=evo-agents", "number=2", "approved=true", 'claims=["CL-90"]', "note=a=b"]) == {
        "canvas": "evo-agents",
        "number": 2,
        "approved": True,
        "claims": ["CL-90"],
        "note": "a=b",
    }
    with pytest.raises(WorkerStateError, match="KEY=VALUE"):
        payload_contains(["canvas"])
    with pytest.raises(WorkerStateError, match="twice"):
        payload_contains(["a=1", "a=2"])


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = WorkerHome(tmp_path / "worker")
    home.run_dir(RUN).mkdir(parents=True)
    found = types.SimpleNamespace(home=home, run_id=RUN, record={"kind": "plan"})
    monkeypatch.setattr(worker_cli, "agent_run", lambda command, kinds=("plan",): found)
    return found


def hub_answering(monkeypatch, reply: dict) -> list:
    sent = []

    class Hub:
        async def wait(self, run_id, body):
            sent.append(("wait", run_id, body))
            return reply

        async def decision(self, run_id, body):
            sent.append(("decision", run_id, body))
            return reply

    monkeypatch.setattr(worker_cli, "with_hub", lambda agent, work: asyncio.run(work(Hub())))
    return sent


def wait_args(**given):
    return types.SimpleNamespace(
        **{"reason": "W44", "until": None, "on": None, "contains": None, "step": None, **given}
    )


def test_wait_sends_the_wait_and_notes_it_for_the_daemon(agent, monkeypatch, capsys):
    sent = hub_answering(monkeypatch, {"id": 41, "wake_at": "2026-10-30T09:00:00+00:00"})
    args = wait_args(until="2026-10-30T09:00Z", on="canvas.baseline", contains=["canvas=evo-agents"], step="23")
    assert worker_cli.cmd_wait(args) == 0
    assert sent == [
        (
            "wait",
            RUN,
            {
                "reason": "W44",
                "until": "2026-10-30T09:00:00+00:00",
                "on": "canvas.baseline",
                "contains": {"canvas": "evo-agents"},
                "step_key": "23",
            },
        )
    ]
    assert json.loads(agent.home.decisions_path(RUN).read_text(encoding="utf-8"))["id"] == 41
    out = capsys.readouterr().out
    assert "Wait #41 is open: the run wakes at 2026-10-30 09:00 UTC at the latest, or sooner on an event" in out
    assert worker_cli.cmd_wait(wait_args()) == 1
    assert "(--until)" in capsys.readouterr().err
    assert worker_cli.cmd_wait(wait_args(until="+1h", contains=["a=1"])) == 1
    assert "--contains filters" in capsys.readouterr().err
    assert len(sent) == 1


def ask_args(**given):
    base = {
        "category": "scope",
        "question": "Approved W44?",
        "option": ["yes=Approved", "later=Not yet"],
        "context_file": None,
        "recommended": None,
        "step": None,
        "attach": None,
        "wait_option": None,
    }
    return types.SimpleNamespace(**{**base, **given})


def test_ask_marks_the_options_that_wait(agent, monkeypatch, capsys):
    sent = hub_answering(monkeypatch, {"id": 42})
    monkeypatch.setattr(worker_cli.output_cli, "attached", lambda home, run_id, files: {})
    assert worker_cli.cmd_ask(ask_args(wait_option=["later"])) == 0
    ((_, _, body),) = sent
    assert body["options"] == [{"key": "yes", "label": "Approved"}, {"key": "later", "label": "Not yet", "waits": True}]
    assert worker_cli.cmd_ask(ask_args(wait_option=["maybe"])) == 1
    assert "names one of the options" in capsys.readouterr().err
    assert worker_cli.cmd_ask(ask_args(wait_option=["yes", "later"])) == 1
    assert "names every one" in capsys.readouterr().err
    assert len(sent) == 1
