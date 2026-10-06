"""``evo-agents hub run``: dispatch, list, show, logs (and --follow over the run's server-sent events), send and the
owner's controls, against a hub under uvicorn, as a signed-in member runs them.

The checks step 12 of the worker-fleet plan names: every command takes the id of a run and prints what the hub
answered, ``--json`` with the keys the contract declares (seam hub-cli-v1); ``logs --follow`` reads the stream until
its ``end`` event and does not open it again after that. Around them: the SSE parsing, reading on after a stream that
broke off with Last-Event-ID, giving up after tries that bring nothing, the refusals the hub answers, and a command
without a project or a sign-in.

Step 9 of the plan-runs-and-decisions plan adds ``run plan`` (a plan run, its model and its timeout in hours), the
KIND column of ``run list``, ``--model`` for ``run dispatch`` (only with a runtime named), ``hub decision list|show|
answer`` and ``hub notifications [--unread] [--read all]``, each ``--json`` with the keys the contract declares.

The SSE parsing, the reconnects (with a fake stream) and the usage errors run without Postgres; everything that needs
the hub skips without EVO_HUB_TEST_DSN."""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.cli import main
from evo_agents.hub import run_cli
from evo_agents.hub.client import Hub, HubError, Unreachable
from evo_agents.hub.run_cli import follow, format_event, sse_messages
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
HUB_URL = "https://hub.example.org"
AT = "2026-10-05T09:12:03.120000+07:00"


def write_credentials(home: Path, url: str, login: str, token: str) -> Path:
    """A home whose ~/.evo/hub holds a hub token, as `hub login` leaves it."""
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")
    for name in ("token", "config.json"):
        os.chmod(directory / name, 0o600)
    return home


def cli(monkeypatch, capsys, home: Path, *args: str, stdin: str | None = None) -> SimpleNamespace:
    """``evo-agents ARGS`` in this process, as the member whose credentials ``home`` holds."""
    with monkeypatch.context() as patched:
        patched.setenv("HOME", str(home))
        if stdin is not None:
            patched.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(list(args))
    out, err = capsys.readouterr()
    return SimpleNamespace(code=code, out=out, err=err)


def ok(result: SimpleNamespace) -> SimpleNamespace:
    assert result.code == 0, result.out + result.err
    return result


def as_json(result: SimpleNamespace):
    return json.loads(ok(result).out)


# Reading server-sent events


def test_sse_messages_reads_the_stream_as_the_standard_says():
    stream = [
        b": ping\n",
        b"\n",
        b"id: 3\n",
        b'data: {"seq": 3}\n',
        b"\n",
        b"event: end\r\n",
        b"data:first line\r\n",
        b"data: second line\r\n",
        b"retry: 1000\r\n",
        b"\r\n",
        b"event: lonely\n",  # a message without data is dropped
        b"\n",
        b"id: 9\rdata: x\r\r",  # lone carriage returns end lines too
        b"data: never finished\n",  # the stream ended before the blank line: dropped
    ]
    assert list(sse_messages(stream)) == [
        ("message", '{"seq": 3}', "3"),
        ("end", "first line\nsecond line", "3"),
        ("message", "x", "9"),
    ]


def test_an_event_is_one_line_with_seq_time_kind_and_text():
    state = {"seq": 2, "at": AT, "kind": "state", "body": {"from": "leased", "to": "running", "actor": "worker"}}
    assert format_event(state) == "    2 02:12:03 state   leased -> running by worker"
    reason = {**state, "body": {**state["body"], "to": "failed", "reason": "ruff found 3 problems"}}
    assert format_event(reason).endswith("leased -> failed by worker: ruff found 3 problems")
    chunk = {"seq": 41, "at": AT, "kind": "agent_message_chunk", "body": {"text": "Two lines\nof text"}}
    assert format_event(chunk) == "   41 02:12:03 agent   Two lines\n                       of text"
    acp = {**chunk, "body": {"content": {"type": "text", "text": "DONE"}}}  # as the runtime adapters send it
    assert format_event(acp) == "   41 02:12:03 agent   DONE"
    message = {"seq": 5, "at": AT, "kind": "user_message", "body": {"text": "also run ruff", "from": "owner"}}
    assert format_event(message).endswith("message owner: also run ruff")
    tool = {"seq": 6, "at": AT, "kind": "tool_call", "body": {"title": "Bash: pytest -q", "status": "pending"}}
    assert format_event(tool).endswith("tool    Bash: pytest -q (pending)")
    raw = {"seq": 7, "at": "not a time", "kind": "output", "body": {"type": "x" * 400}}
    line = format_event(raw)
    assert line.startswith('    7 --:--:-- output  {"type":"xxx') and line.endswith("...")
    assert len(line) == len("    7 --:--:-- output  ") + run_cli.RAW_CHARS


# Following a stream, against a fake one


def sse(*messages: dict, end: dict | None = None) -> list[bytes]:
    lines = [b": ping\n", b"\n"]
    for message in messages:
        lines += [f"id: {message['seq']}\n".encode(), f"data: {json.dumps(message)}\n".encode(), b"\n"]
    if end is not None:
        lines += [b"event: end\n", f"data: {json.dumps(end)}\n".encode(), b"\n"]
    return lines


def chunk(seq: int, kind: str = "agent_message_chunk") -> dict:
    return {"seq": seq, "at": AT, "kind": kind, "body": {"text": f"chunk {seq}"}, "truncated": False}


class FakeStreams:
    """Stands for ``run_cli.open_stream``: each opening takes the next of ``answers``, a list of lines or an
    exception, and records the path and Last-Event-ID it was opened with."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.opened: list[tuple[str, int | None]] = []

    @contextmanager
    def __call__(self, hub, path: str, last_event_id: int | None = None):
        self.opened.append((path, last_event_id))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        yield iter(answer)


def run_follow(streams: FakeStreams, after: int = 0, kinds=frozenset()):
    seen, sleeps, notes = [], [], []
    ended = follow(
        Hub(HUB_URL, "evh_test"),
        "demo",
        12,
        after,
        on_event=seen.append,
        kinds=kinds,
        opener=streams,
        sleep=sleeps.append,
        warn=notes.append,
    )
    return ended, [event["seq"] for event in seen], sleeps, notes


def test_follow_reads_on_after_the_last_event_and_stops_at_the_end_event():
    streams = FakeStreams(
        sse(chunk(4), chunk(5)),  # closed by a proxy before the run ended
        Unreachable("cannot reach https://hub.example.org: connection reset"),
        sse(chunk(6), end={"state": "done", "last_seq": 6}),
        AssertionError("opened again after the end event"),
    )
    ended, seqs, sleeps, notes = run_follow(streams, after=3)
    assert ended == {"state": "done", "last_seq": 6}
    assert seqs == [4, 5, 6]
    path = "/v1/projects/demo/runs/12/stream"
    assert streams.opened == [(f"{path}?after=3", None), (path, 5), (path, 5)]
    assert sleeps == [1, 2]
    assert notes == [
        "note: the hub closed the stream before the run ended; reading on after event 5 in 1s",
        "note: cannot reach https://hub.example.org: connection reset; reading on after event 5 in 2s",
    ]
    assert len(streams.answers) == 1


def test_follow_shows_only_the_kinds_asked_for_but_reads_on_after_every_event():
    streams = FakeStreams(sse(chunk(1), chunk(2, "system")), sse(chunk(3), end={"state": "failed", "last_seq": 3}))
    _, seqs, _, _ = run_follow(streams, kinds=frozenset({"agent_message_chunk"}))
    assert seqs == [1, 3]
    assert streams.opened[1] == ("/v1/projects/demo/runs/12/stream", 2)


def test_follow_gives_up_after_tries_in_a_row_that_bring_nothing():
    down = [Unreachable("cannot reach https://hub.example.org: refused", refused=True)] * (run_cli.RECONNECTS + 1)
    with pytest.raises(HubError) as raised:
        run_follow(FakeStreams(sse(chunk(1)), *down))
    assert "stopped 6 times in a row without bringing anything" in str(raised.value)
    assert "`evo-agents hub run logs 12 --after 1` reads what came since" in str(raised.value)


def test_follow_counts_again_after_a_stream_that_brought_a_ping():
    refused = Unreachable("cannot reach https://hub.example.org: refused", refused=True)
    ping = [b": ping\n", b"\n"]
    tries = run_cli.RECONNECTS
    # the first opening and five more fail; the sixth brings a ping, then the stream breaks and four more fail
    streams = FakeStreams(
        *[refused] * tries, ping, *[refused] * (tries - 1), sse(end={"state": "cancelled", "last_seq": 0})
    )
    ended, _, sleeps, _ = run_follow(streams)
    assert ended["state"] == "cancelled"
    assert sleeps == [1, 2, 4, 8, 15, 1, 2, 4, 8, 15]
    streams = FakeStreams(*[refused] * tries, ping, *[refused] * tries)
    with pytest.raises(HubError, match="stopped 6 times in a row"):
        run_follow(streams)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_follow_never_retries_a_refusal(status):
    refusal = HubError("project demo has no run 12", status, "not_found")
    streams = FakeStreams(refusal, AssertionError("retried"))
    with pytest.raises(HubError, match="has no run 12"):
        run_follow(streams)
    assert len(streams.opened) == 1


def test_follow_retries_a_proxy_that_answers_503():
    streams = FakeStreams(HubError("the hub answered HTTP 503", 503), sse(end={"state": "done", "last_seq": 9}))
    assert run_follow(streams)[0] == {"state": "done", "last_seq": 9}


# Usage, without a hub


def test_json_does_not_go_with_follow(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = cli(monkeypatch, capsys, home, "hub", "run", "logs", "12", "--follow", "--json", "--project", "demo")
    assert result.code == 2 and result.out == ""
    assert result.err == "error: --json prints the events read as one object, so it does not go with --follow\n"


def test_a_command_without_a_sign_in_or_a_project_says_what_to_do(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    outside = cli(monkeypatch, capsys, tmp_path / "nobody", "hub", "run", "show", "12", "--project", "demo")
    assert outside.code == 1 and outside.err == "error: not signed in to a hub: run `evo-agents hub login --url URL`\n"
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    lost = cli(monkeypatch, capsys, home, "hub", "run", "show", "12")
    assert lost.code == 1 and "which project? pass --project" in lost.err


def test_a_run_is_named_by_its_number():
    with pytest.raises(SystemExit):
        main(["hub", "run", "show", "first", "--project", "demo"])
    with pytest.raises(SystemExit):
        main(["hub", "run", "dispatch", "rollout", "2", "--timeout", "300", "--project", "demo"])
    with pytest.raises(SystemExit):
        main(["hub", "run", "plan", "rollout", "--timeout-h", "3", "--project", "demo"])
    with pytest.raises(SystemExit):
        main(["hub", "decision", "show", "first", "--project", "demo"])


@pytest.mark.parametrize(
    "args",
    [
        ("run", "dispatch", "rollout", "2", "--model", "opus"),
        ("run", "dispatch", "rollout", "2", "--runtime", "any", "--model", "opus"),
        ("run", "plan", "rollout", "--model", "opus"),
    ],
)
def test_a_model_needs_the_runtime_that_names_it(monkeypatch, capsys, tmp_path, args):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = cli(monkeypatch, capsys, home, "hub", *args, "--project", "demo")
    assert result.code == 2 and result.out == ""
    assert result.err.startswith("error: --model names a model as one runtime names it, so it needs --runtime")


@pytest.mark.parametrize(
    "args, problem",
    [
        (("decision", "answer", "7"), "an answer names an option with --option KEY, gives words with --text TEXT"),
        (("notifications", "--read", "all", "--unread"), "--read marks notifications read and lists none"),
        (("notifications", "--read", "all", "--offset", "0"), "so it does not go with --offset"),
        (("notifications", "--read", "12,x"), "--read takes all, or notification ids such as 12,14, not '12,x'"),
    ],
)
def test_answers_and_reads_that_say_nothing_are_usage_errors(monkeypatch, capsys, tmp_path, args, problem):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = cli(monkeypatch, capsys, home, "hub", *args)
    assert result.code == 2 and result.out == ""
    assert result.err.startswith("error: ") and problem in result.err


# Against a hub

if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_decisions import ANSWER_TEXT, OPTIONS, QUESTION, asked
    from tests.hub.test_plan_runs import PLAN as FLEET
    from tests.hub.test_plan_runs import REPOS, dispatched_plan, fleet_worker, push
    from tests.hub.test_plan_runs import plan_body as fleet_body
    from tests.hub.test_run_stream import event, members, sent, serving
    from tests.hub.test_runs import (
        OTHER,
        OWNER,
        PASSED,
        PLAN,
        PROJECT,
        READER,
        SHA,
        add_worker,
        claim,
        dispatched,
        moved,
        step,
    )


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn with project evo-agents and plan rollout (``tests.hub.test_run_stream.members``), and a
    home signed in to it for owner, someone-else and reader."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = members(client, github)
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("other", OTHER), ("reader", READER))
        }
        yield SimpleNamespace(url=url, client=client, headers=headers, homes=homes, db=hub_db)


def runs_of(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return cli(monkeypatch, capsys, hub.homes[who], "hub", "run", *args, "--project", PROJECT)


@needs_pg
def test_dispatch_list_and_show_print_what_the_hub_answered(hub, monkeypatch, capsys):
    worker = add_worker(hub.client, hub.headers["owner"], "mac-mini")
    flags = ["--runtime", "claude-code", "--mode", "headless", "--approval", "auto", "--timeout", "30"]
    queued = as_json(
        runs_of(hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "4", "5", *flags, "--worker", "mac-mini", "--json")
    )
    assert_json_keys("hub run dispatch", queued)
    assert [(r["step_key"], r["title"], r["state"]) for r in queued] == [
        ("4", "Docs", "queued"),
        ("5", "Web", "queued"),
    ]
    for run in queued:
        assert (run["requested_runtime"], run["mode"], run["approval"], run["timeout_min"]) == (
            "claude-code",
            "headless",
            "auto",
            30,
        )
        assert (run["pinned_worker_id"], run["dispatched_by"], run["repo"]) == (worker["id"], OWNER, "evo-agents")
    by_id = as_json(runs_of(hub, monkeypatch, capsys, "owner", "show", str(queued[0]["id"]), "--json"))
    assert by_id == queued[0]

    printed = ok(runs_of(hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "2"))
    two = queued[-1]["id"] + 1
    assert printed.out.splitlines() == [
        f"Queued run #{two}: step 2 (Queue) of plan rollout, repo evo-agents; runtime any, headless, approval review, "
        "timeout 60 min.",
        f"Follow it with `evo-agents hub run logs {two} --follow --project {PROJECT}`.",
    ]
    again = runs_of(hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "2", "4")
    assert again.code == 1 and again.out == ""
    assert (
        again.err.startswith("error: step 2 of plan rollout is not ready: ") and "nothing was dispatched" in again.err
    )
    nobody = runs_of(hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "2", "--worker", "nope")
    assert nobody.code == 1 and "you have no worker named 'nope'" in nobody.err
    pinned_elsewhere = runs_of(hub, monkeypatch, capsys, "other", "dispatch", PLAN, "5", "--worker", str(worker["id"]))
    assert (
        pinned_elsewhere.code == 1
        and "a run goes only to a worker of the member who dispatches it" in pinned_elsewhere.err
    )
    reader = runs_of(hub, monkeypatch, capsys, "reader", "dispatch", PLAN, "5")
    assert reader.code == 1 and reader.err == (
        f"error: dispatching runs in project {PROJECT} needs the writer role on it; you hold reader\n"
    )

    listed = as_json(runs_of(hub, monkeypatch, capsys, "reader", "list", "--json"))
    assert_json_keys("hub run list", listed)
    assert_json_keys("hub run dispatch", listed["runs"])
    assert [run["id"] for run in listed["runs"]] == [two, queued[1]["id"], queued[0]["id"]]
    assert (listed["total"], listed["counts"]["queued"], listed["limit"], listed["offset"]) == (3, 3, 50, 0)
    filtered = as_json(
        runs_of(
            hub,
            monkeypatch,
            capsys,
            "reader",
            "list",
            "--state",
            "queued",
            "--state",
            "done",
            "--plan",
            PLAN,
            "--step",
            "4",
            "--by",
            OWNER.upper(),
            "--json",
        )
    )
    assert [run["id"] for run in filtered["runs"]] == [queued[0]["id"]]
    assert as_json(runs_of(hub, monkeypatch, capsys, "reader", "list", "--search", "docs", "--json"))["total"] == 1
    mine = as_json(runs_of(hub, monkeypatch, capsys, "owner", "list", "--worker", "mac-mini", "--json"))
    assert mine["total"] == 0  # none claimed yet

    table = ok(runs_of(hub, monkeypatch, capsys, "reader", "list", "--limit", "2"))
    lines = table.out.splitlines()
    header = ["RUN", "KIND", "STATE", "PLAN", "STEP", "TITLE", "RUNTIME", "WORKER", "BY", "QUEUED", "(UTC)"]
    assert lines[0].split() == header
    assert lines[1].split()[:9] == [f"#{two}", "step", "queued", PLAN, "2", "Queue", "any", "-", OWNER]
    assert lines[2].split()[:9] == [
        f"#{queued[1]['id']}",
        "step",
        "queued",
        PLAN,
        "5",
        "Web",
        "claude-code",
        "-",
        OWNER,
    ]
    assert lines[3:] == [f"2 of 3 run(s) of project {PROJECT}; by state: 3 queued", "More with --offset 2."]

    shown = ok(runs_of(hub, monkeypatch, capsys, "reader", "show", str(queued[0]["id"]))).out.splitlines()
    assert shown[0] == f"Run #{queued[0]['id']}"
    fields = {line.split()[0]: " ".join(line.split()[1:]) for line in shown[1:] if line.startswith("  ")}
    assert fields["state"] == "queued, attempt 1 of 3"
    assert fields["step"] == "4: Docs"
    assert fields["plan"] == f"{PLAN} of project {PROJECT}, revision 1"
    assert fields["worker"] == f"none yet, pinned to worker #{worker['id']}"
    assert fields["runtime"] == "claude-code, headless, approval auto, timeout 30 min"
    missing = runs_of(hub, monkeypatch, capsys, "reader", "show", "999999")
    assert missing.code == 1 and missing.err.startswith(f"error: project {PROJECT} has no run 999999")


def started(hub, step_key: int = 2, **dispatch) -> tuple[dict, int]:
    """A worker of owner and a run of ``step_key`` it claimed and started: its state events are seq 1 and 2."""
    worker = add_worker(hub.client, hub.headers["owner"], f"mac-{step_key}")
    run_id = dispatched(hub.client, hub.headers["owner"], [step_key], **dispatch)[0]["id"]
    assert claim(hub.client, worker)["id"] == run_id
    moved(hub.client, worker, run_id, "running")
    return worker, run_id


@needs_pg
def test_logs_reads_every_page_of_events(hub, monkeypatch, capsys):
    worker, run_id = started(hub)
    sent(hub, worker, run_id, *(event(n) for n in range(1, 4)))  # hub seqs 3 to 5

    printed = ok(runs_of(hub, monkeypatch, capsys, "reader", "logs", str(run_id))).out.splitlines()
    assert [line.split()[0] for line in printed[:5]] == ["1", "2", "3", "4", "5"]
    assert printed[0].endswith("state   queued -> leased by worker: worker mac-2 claimed it")
    assert printed[1].endswith("state   leased -> running by worker: worker mac-2 reported running")
    assert printed[2].split()[2:] == ["agent", "chunk", "1"]
    assert printed[5] == (
        f"Run #{run_id} is running: 5 event(s) shown, the last of the run is 5; --follow prints the next ones as "
        "they come."
    )

    monkeypatch.setattr(run_cli, "EVENTS_PAGE", 2)  # three pages
    page = as_json(runs_of(hub, monkeypatch, capsys, "reader", "logs", str(run_id), "--json"))
    assert_json_keys("hub run logs", page)
    assert (page["run_id"], page["state"], page["last_seq"], page["more"]) == (run_id, "running", 5, False)
    assert [e["seq"] for e in page["events"]] == [1, 2, 3, 4, 5]
    later = as_json(
        runs_of(
            hub,
            monkeypatch,
            capsys,
            "reader",
            "logs",
            str(run_id),
            "--after",
            "3",
            "--kind",
            "agent_message_chunk",
            "--json",
        )
    )
    assert [(e["seq"], e["body"]["text"]) for e in later["events"]] == [(4, "chunk 2"), (5, "chunk 3")]
    claimed = as_json(runs_of(hub, monkeypatch, capsys, "owner", "list", "--worker", "mac-2", "--json"))
    assert [run["id"] for run in claimed["runs"]] == [run_id]
    stranger = runs_of(hub, monkeypatch, capsys, "reader", "logs", "999999")
    assert stranger.code == 1 and "has no run 999999" in stranger.err


def lines_of(stream) -> "queue.Queue[str | None]":
    """The lines ``stream`` gives, in a queue a thread fills; None once it ends."""
    lines: queue.Queue = queue.Queue()

    def pump():
        for line in stream:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return lines


def next_line(lines: "queue.Queue[str | None]", timeout: float = 10.0) -> str | None:
    return lines.get(timeout=timeout)


@needs_pg
def test_logs_follow_prints_events_as_they_come_and_stops_at_the_end_of_the_run(hub):
    worker, run_id = started(hub, approval="auto")
    sent(hub, worker, run_id, event(1, "before"))  # hub seq 3
    env = pg.clean_env(HOME=str(hub.homes["reader"]))
    command = [sys.executable, "-m", "evo_agents", "hub", "run", "logs", str(run_id), "-f", "--after", "1"]
    proc = subprocess.Popen(
        [*command, "--project", PROJECT], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        out = lines_of(proc.stdout)
        first = next_line(out)
        assert first.split()[0] == "2" and first.endswith(
            "state   leased -> running by worker: worker mac-2 reported running"
        )
        second = next_line(out)
        assert second.split()[0] == "3" and second.endswith("agent   before")
        sent_at = time.monotonic()
        sent(hub, worker, run_id, event(2, "while it runs"))  # hub seq 4
        live_line = next_line(out)
        assert time.monotonic() - sent_at < 2, "the followed event came late"
        assert live_line.split()[0] == "4" and live_line.endswith("agent   while it runs")
        moved(hub.client, worker, run_id, "verifying", "done", commit_sha=SHA, verify=PASSED)
        rest = []
        while (line := next_line(out)) is not None:
            rest.append(line)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert [line.split()[2:6] for line in rest[:2]] == [
        ["state", "running", "->", "verifying"],
        ["state", "verifying", "->", "done"],
    ]
    assert rest[2:] == [f"Run #{run_id} ended done; its last event is 6."]
    assert proc.stderr.read() == ""  # the stream ended with its end event: no reconnect, no note
    proc.stderr.close()
    proc.stdout.close()


@needs_pg
def test_the_owner_steers_and_ends_a_run_and_others_are_refused(hub, monkeypatch, capsys):
    worker, run_id = started(hub)
    run = str(run_id)

    message = as_json(runs_of(hub, monkeypatch, capsys, "owner", "send", run, "also run ruff", "--json"))
    assert_json_keys("hub run send", message)
    assert (message["run_id"], message["seq"], message["text"], message["sent_by"]) == (
        run_id,
        3,
        "also run ruff",
        OWNER,
    )
    piped = ok(
        cli(
            monkeypatch,
            capsys,
            hub.homes["owner"],
            "hub",
            "run",
            "send",
            run,
            "-",
            "--project",
            PROJECT,
            stdin="from stdin\n",
        )
    )
    assert (
        piped.out
        == f"Sent message #{message['id'] + 1} to run #{run_id} (event 4); its worker hands it to the agent.\n"
    )
    refused = runs_of(hub, monkeypatch, capsys, "reader", "send", run, "stop")
    assert refused.code == 1 and "only owner" in refused.err

    asked = ok(runs_of(hub, monkeypatch, capsys, "owner", "takeover", run))
    assert asked.out.startswith(f"Asked the worker of run #{run_id} for a takeover")
    shown = as_json(runs_of(hub, monkeypatch, capsys, "owner", "show", run, "--json"))
    assert shown["takeover_requested_at"] is not None and shown["state"] == "running"
    moved(hub.client, worker, run_id, "interactive")
    handed = as_json(runs_of(hub, monkeypatch, capsys, "owner", "handback", run, "--json"))
    assert_json_keys("hub run handback", handed)
    assert handed["handback_requested_at"] is not None
    early = runs_of(hub, monkeypatch, capsys, "owner", "approve", run)
    assert early.code == 1 and "only a run in review is approved" in early.err
    moved(hub.client, worker, run_id, "running", "verifying", "review", commit_sha=SHA, verify=PASSED)
    other = runs_of(hub, monkeypatch, capsys, "other", "approve", run)
    assert other.code == 1 and f"only {OWNER}, who dispatched run {run_id}, may approve it" in other.err
    approved = ok(runs_of(hub, monkeypatch, capsys, "owner", "approve", run))
    assert approved.out == f"Approved run #{run_id}: it is done, and so is step 2 (Queue) of plan {PLAN}.\n"
    assert step(hub.client, hub.headers["owner"], 2)["status"] == "done"

    queued = dispatched(hub.client, hub.headers["owner"], [4])[0]["id"]
    cancelled = ok(runs_of(hub, monkeypatch, capsys, "owner", "cancel", str(queued)))
    assert cancelled.out == f"Cancelled run #{queued}, step 4 (Docs) of plan {PLAN}.\n"
    again = as_json(runs_of(hub, monkeypatch, capsys, "owner", "rerun", str(queued), "--json"))
    assert_json_keys("hub run rerun", again)
    assert (again["parent_run_id"], again["step_key"], again["state"]) == (queued, "4", "queued")
    assert claim(hub.client, worker)["id"] == again["id"]
    moved(hub.client, worker, again["id"], "running")
    stopping = ok(runs_of(hub, monkeypatch, capsys, "owner", "cancel", str(again["id"])))
    assert stopping.out == (
        f"Asked the worker of run #{again['id']} (running) to stop it; it ends cancelled once the worker does.\n"
    )
    after = as_json(runs_of(hub, monkeypatch, capsys, "owner", "cancel", str(again["id"]), "--json"))
    assert_json_keys("hub run cancel", after)
    assert after["cancel_requested_at"] is not None and after["state"] == "running"
    finished = runs_of(hub, monkeypatch, capsys, "owner", "rerun", run)
    assert finished.code == 1 and "step 2 of plan rollout is not ready: " in finished.err


# Plan runs, decisions and notifications


def hub_of(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return cli(monkeypatch, capsys, hub.homes[who], "hub", *args)


def decisions_of(hub, monkeypatch, capsys, who: str, *args: str, stdin: str | None = None) -> SimpleNamespace:
    return cli(monkeypatch, capsys, hub.homes[who], "hub", "decision", *args, "--project", PROJECT, stdin=stdin)


def fields_of(out: str) -> dict:
    """The ``  label  text`` lines of ``run show`` or ``decision show``, by their first word."""
    return {line.split()[0]: " ".join(line.split()[1:]) for line in out.splitlines() if line.startswith("  ")}


@needs_pg
def test_a_plan_run_is_dispatched_listed_and_shown(hub, monkeypatch, capsys):
    push(hub.client, hub.headers["owner"], fleet_body())
    worker = fleet_worker(hub.client, hub.headers["owner"], "mac-plan")
    refused = runs_of(hub, monkeypatch, capsys, "owner", "plan", FLEET, "--model", "opus")
    assert refused.code == 2 and "needs --runtime with that runtime" in refused.err

    flags = ["--runtime", "claude-code", "--model", "opus", "--mode", "headless", "--timeout-h", "8"]
    run = as_json(runs_of(hub, monkeypatch, capsys, "owner", "plan", FLEET, *flags, "--worker", "mac-plan", "--json"))
    assert_json_keys("hub run plan", run)
    assert (run["kind"], run["plan_id"], run["step_key"], run["title"], run["state"]) == (
        "plan",
        FLEET,
        None,
        "Run the fleet",
        "queued",
    )
    assert (run["requested_runtime"], run["model"], run["mode"], run["timeout_min"]) == (
        "claude-code",
        "opus",
        "headless",
        480,
    )
    assert (run["pinned_worker_id"], run["dispatched_by"], run["repos"]) == (worker["id"], OWNER, REPOS)
    busy = runs_of(hub, monkeypatch, capsys, "owner", "plan", FLEET)
    assert busy.code == 1 and busy.err == (
        f"error: plan {FLEET} has plan run #{run['id']}, queued, dispatched by {OWNER}; nothing was dispatched\n"
    )
    reader = runs_of(hub, monkeypatch, capsys, "reader", "plan", FLEET)
    assert reader.code == 1 and "needs the writer role" in reader.err

    table = ok(runs_of(hub, monkeypatch, capsys, "reader", "list", "--plan", FLEET)).out.splitlines()
    assert table[0].split()[:3] == ["RUN", "KIND", "STATE"]
    assert table[1].split()[:8] == [f"#{run['id']}", "plan", "queued", FLEET, "-", "Run", "the", "fleet"]
    assert table[2] == f"1 of 1 run(s) of project {PROJECT}; by state: 1 queued"

    assert claim(hub.client, worker)["id"] == run["id"]
    moved(hub.client, worker, run["id"], "running")
    shown = ok(runs_of(hub, monkeypatch, capsys, "reader", "show", str(run["id"]))).out
    fields = fields_of(shown)
    assert fields["kind"] == "plan run: every step of the plan not done yet, in one session"
    assert fields["title"] == "Run the fleet"
    assert fields["plan"] == f"{FLEET} of project {PROJECT}, revision 1"
    assert fields["repos"] == "evo-agents (feat/plan-runs), agent-skills (main)"
    assert fields["worker"] == f"mac-plan (#{worker['id']}), pinned to worker #{worker['id']}"
    assert fields["runtime"] == "claude-code, model opus, headless, timeout 8 h"
    assert shown.splitlines()[-1] == (
        f"Its decisions: `evo-agents hub decision list --run {run['id']} --project {PROJECT}`."
    )

    cancelled = ok(runs_of(hub, monkeypatch, capsys, "owner", "cancel", str(run["id"])))
    assert cancelled.out.startswith(f"Asked the worker of run #{run['id']} (running) to stop it")
    moved(hub.client, worker, run["id"], "cancelled")
    printed = ok(runs_of(hub, monkeypatch, capsys, "owner", "plan", FLEET)).out.splitlines()
    second = run["id"] + 1
    assert printed == [
        f"Queued plan run #{second}: plan {FLEET} (Run the fleet), every step not done yet, in evo-agents "
        "(feat/plan-runs), agent-skills (main); runtime any, headless, timeout 4 h of agent time.",
        f"Follow it with `evo-agents hub run logs {second} --follow --project {PROJECT}`; the decisions its agent "
        f"asks you: `evo-agents hub decision list --run {second} --project {PROJECT}`.",
    ]


@needs_pg
def test_dispatch_passes_the_model_with_its_runtime(hub, monkeypatch, capsys):
    flags = ["--runtime", "opencode", "--model", "anthropic/claude-sonnet"]
    queued = as_json(runs_of(hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "2", *flags, "--json"))
    assert [(run["requested_runtime"], run["model"]) for run in queued] == [("opencode", "anthropic/claude-sonnet")]
    printed = ok(
        runs_of(hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "4", "--runtime", "codex", "--model", "o3")
    )
    assert printed.out.splitlines()[0].endswith(
        "repo evo-agents; runtime codex (model o3), headless, approval review, timeout 60 min."
    )
    shown = fields_of(ok(runs_of(hub, monkeypatch, capsys, "owner", "show", str(queued[0]["id"]))).out)
    assert shown["runtime"] == "opencode, model anthropic/claude-sonnet, headless, approval review, timeout 60 min"


def notice(hub, worker: dict, run_id: int) -> dict:
    body = {
        "kind": "push_default_branch",
        "title": "Pushed main of agent-skills",
        "repo": "agent-skills",
        "branch": "main",
        "commits": [SHA],
    }
    response = hub.client.post(f"/v1/worker/runs/{run_id}/notices", json=body, headers=worker["headers"])
    assert response.status_code == 201, response.text
    return response.json()


@needs_pg
def test_the_owner_reads_and_answers_the_decisions_of_a_plan_run(hub, monkeypatch, capsys):
    push(hub.client, hub.headers["owner"], fleet_body())
    worker = fleet_worker(hub.client, hub.headers["owner"], "mac-plan")
    run_id = dispatched_plan(hub.client, hub.headers["owner"])["id"]
    assert claim(hub.client, worker)["id"] == run_id
    moved(hub.client, worker, run_id, "running")
    decision = asked(hub.client, worker, run_id)
    number = str(decision["id"])

    listed = as_json(decisions_of(hub, monkeypatch, capsys, "reader", "list", "--json"))
    assert_json_keys("hub decision list", listed)
    assert_json_keys("hub decision show", listed["decisions"][0])
    assert (listed["total"], listed["limit"], listed["offset"]) == (1, 50, 0)
    filtered = as_json(
        decisions_of(hub, monkeypatch, capsys, "reader", "list", "--state", "answered", "--run", str(run_id), "--json")
    )
    assert filtered["total"] == 0
    table = ok(decisions_of(hub, monkeypatch, capsys, "reader", "list", "--state", "open", "--plan", FLEET))
    lines = table.out.splitlines()
    assert lines[0].split() == [
        "DECISION",
        "STATE",
        "CATEGORY",
        "RUN",
        "PLAN",
        "STEP",
        "OWNER",
        "QUESTION",
        "ASKED",
        "(UTC)",
    ]
    assert lines[1].split()[:8] == [f"#{number}", "open", "architecture", f"#{run_id}", FLEET, "2", OWNER, "Which"]
    assert lines[2:] == [
        f"1 of 1 decision(s) of project {PROJECT}",
        f"Read one with `evo-agents hub decision show ID --project {PROJECT}`, and answer it with "
        f"`evo-agents hub decision answer ID --option KEY --project {PROJECT}`.",
    ]

    shown = ok(decisions_of(hub, monkeypatch, capsys, "reader", "show", number)).out
    assert shown.splitlines()[0] == f"Decision #{number}: architecture, open"
    fields = fields_of(shown.split("Question:")[0])
    assert fields["run"] == f"#{run_id} (running) of plan {FLEET}, step 2"
    assert fields["owner"] == f"{OWNER}, who alone answers it"
    assert shown.split("Question:\n")[1].split("Options:\n")[0] == (
        f"  {QUESTION}\nContext:\n  ## Why\n  The plan leaves it open.\n"
    )
    assert shown.split("Options:\n")[1].splitlines() == [
        f"  sqlite    {OPTIONS[0]['label']}",
        f"  postgres  {OPTIONS[1]['label']} (recommended)",
        f"            {OPTIONS[1]['description']}",
        f"Answer it with `evo-agents hub decision answer {number} --option KEY --project {PROJECT}`; --text adds "
        "words of your own, or stands for an option.",
    ]
    missing = decisions_of(hub, monkeypatch, capsys, "reader", "show", "999999")
    assert missing.code == 1 and missing.err.startswith(f"error: project {PROJECT} has no decision 999999")

    # the owner's notifications: the decision, then a notice of a push
    pushed = notice(hub, worker, run_id)
    inbox = as_json(hub_of(hub, monkeypatch, capsys, "owner", "notifications", "--unread", "--json"))
    assert_json_keys("hub notifications", inbox)
    assert [(n["kind"], n["decision_id"], n["read_at"]) for n in inbox["notifications"]] == [
        ("decision", decision["id"], None),
        ("notice", None, None),
    ]
    printed = ok(hub_of(hub, monkeypatch, capsys, "owner", "notifications")).out.splitlines()
    assert printed[0].split() == ["ID", "READ", "KIND", "PROJECT", "RUN", "TITLE", "WHEN", "(UTC)"]
    assert printed[1].split()[:6] == [
        f"#{inbox['notifications'][0]['id']}",
        "unread",
        "decision",
        f"#{number}",
        "(open)",
        PROJECT,
    ]
    assert printed[2].split()[:7] == [
        f"#{pushed['id']}",
        "unread",
        "push_default_branch",
        PROJECT,
        f"#{run_id}",
        "Pushed",
        "main",
    ]
    assert printed[3:] == [
        "2 of 2 notification(s); 2 unread in all, 1 decision(s) waiting for your answer",
        "Mark them read with `evo-agents hub notifications --read all`, or --read with their ids.",
    ]
    assert as_json(hub_of(hub, monkeypatch, capsys, "reader", "notifications", "--json"))["total"] == 0
    notices = as_json(hub_of(hub, monkeypatch, capsys, "owner", "notifications", "--kind", "notice", "--json"))
    assert [n["id"] for n in notices["notifications"]] == [pushed["id"]]
    marked = as_json(hub_of(hub, monkeypatch, capsys, "owner", "notifications", "--read", str(pushed["id"]), "--json"))
    assert_json_keys("hub notifications", marked, "--read")
    assert marked == {"read": 1, "unread": 1}

    # answering: another member, an option the decision does not have, then the owner
    other = decisions_of(hub, monkeypatch, capsys, "other", "answer", number, "--option", "postgres")
    assert other.code == 1 and f"only {OWNER}, who dispatched its run, may answer decision {number}" in other.err
    wrong = decisions_of(hub, monkeypatch, capsys, "owner", "answer", number, "--option", "mysql")
    assert wrong.code == 1 and "has no option 'mysql'" in wrong.err
    answered = ok(
        decisions_of(
            hub,
            monkeypatch,
            capsys,
            "owner",
            "answer",
            number,
            "--option",
            "postgres",
            "--text",
            "-",
            stdin=ANSWER_TEXT,
        )
    )
    assert answered.out.splitlines() == [
        f"Answered decision #{number} with option postgres (Move to Postgres) and your words.",
        f"The answer went to the inbox of run #{run_id}; its worker hands it to the agent.",
    ]
    again = decisions_of(hub, monkeypatch, capsys, "owner", "answer", number, "--text", "and keep it")
    assert again.code == 1 and f"decision {number} is answered, not open" in again.err
    after = as_json(decisions_of(hub, monkeypatch, capsys, "owner", "show", number, "--json"))
    assert_json_keys("hub decision answer", after)
    assert (after["state"], after["answer_option"], after["answer_text"], after["answered_by"]) == (
        "answered",
        "postgres",
        ANSWER_TEXT,
        OWNER,
    )
    assert after["answer_run_id"] == run_id
    fields = fields_of(ok(decisions_of(hub, monkeypatch, capsys, "owner", "show", number)).out.split("Question:")[0])
    assert fields["option"] == "postgres, Move to Postgres"
    assert fields["text"] == ANSWER_TEXT
    assert fields["delivered"] == "not to the agent yet"
    left = ok(hub_of(hub, monkeypatch, capsys, "owner", "notifications", "--read", "all"))
    assert left.out == "Marked 0 notification(s) read; 0 left unread.\n"  # answering read the decision's
