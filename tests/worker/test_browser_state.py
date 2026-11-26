"""The agent's side of a login on the worker (plan agent-browser, criterion a8): ``evo-agents worker browser state`` writes
the cookies of the run's Chromium into the run's own directory and refuses a worktree; ``worker browser login`` notes
the decision it asked for the daemon, which saves the profile at the owner's Done and at nothing else."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import types

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import browser_login
from evo_agents.worker.browser import logins, state
from evo_agents.worker.cli.browser import BROWSER_KINDS, LOGIN, STATE
from evo_agents.worker.home import WorkerHome, WorkerStateError
from evo_agents.worker.runner.agent import Agent

RUN = 7
COOKIE = {
    "name": "sid",
    "value": "s3cret",
    "domain": ".northwind.example",
    "path": "/",
    "expires": 1893456000.5,
    "session": False,
    "httpOnly": True,
    "secure": True,
    "sameSite": "Strict",
}


class Run:
    """The context of a browser command inside run RUN of ``home``: its hub is ``hub``, when given."""

    def __init__(self, home, args, hub=None):
        self.home, self.args, self.hub, self.run_id = home, args, hub, RUN
        self.config = self.token = None
        self.out = sys.stdout

    def check(self):
        pass

    def with_hub(self, work):
        return asyncio.run(work(self.hub))


def ran(command, context) -> int:
    """Run ``command`` in ``context`` and print its result, as the command line does."""
    result = command.run(context)
    result.show(sys.stdout, False)
    return result.status


def answer(decision_id: int, key: str | None) -> dict:
    chosen = f"Chosen option: {key}, {key.title()}." if key else "Not a chosen option."
    return {"decision_id": decision_id, "text": f"Decision #{decision_id} answered.\n{chosen}\nThe rest."}


@pytest.fixture
def home(tmp_path) -> WorkerHome:
    found = WorkerHome(tmp_path / "evo")
    found.ensure()
    found.run_dir(RUN).mkdir(parents=True)
    return found


def test_browser_state_goes_only_where_the_run_owns_it_and_never_into_a_worktree(home, tmp_path):
    base = home.run_dir(RUN).resolve()
    assert state.out_path(home, RUN, "session.json") == base / "session.json"
    assert state.out_path(home, RUN, str(base / "session.json")) == base / "session.json"
    worktree = home.worktree_path("demo", RUN)
    worktree.mkdir()
    for refused, said in (
        (str(worktree / "state.json"), "worktree"),
        (str(home.worktrees_dir / "demo-7" / "sub" / "state.json"), "worktree"),
        (str(tmp_path / "elsewhere.json"), "outside the run's directory"),
        ("../8/state.json", "outside the run's directory"),
    ):
        with pytest.raises(WorkerStateError, match=said):
            state.out_path(home, RUN, refused)
    (base / "link.json").symlink_to(tmp_path / "target")
    with pytest.raises(WorkerStateError, match="link or a directory"):
        state.out_path(home, RUN, "link.json")


def test_browser_state_is_a_playwright_storage_state_readable_by_its_owner_only(home):
    found = state.storage_state([COOKIE, {"name": "tmp", "value": "x", "domain": "a.example", "session": True}])
    assert found["origins"] == []
    assert found["cookies"][0] == {
        "name": "sid",
        "value": "s3cret",
        "domain": ".northwind.example",
        "path": "/",
        "expires": 1893456000.5,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Strict",
    }
    assert found["cookies"][1]["expires"] == -1 and found["cookies"][1]["sameSite"] == "Lax"
    path = state.out_path(home, RUN, "state.json")
    state.write(path, found)
    assert json.loads(path.read_text(encoding="utf-8")) == found
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    state.write(path, {"cookies": [], "origins": []})  # replaced, not appended
    assert json.loads(path.read_text(encoding="utf-8"))["cookies"] == []


def test_browser_state_command_writes_the_cookies_of_the_run_browser(home, monkeypatch, capsys):
    seen = []

    async def read_cookies(cdp):
        seen.append(cdp)
        return [COOKIE]

    monkeypatch.setattr(state, "read_cookies", read_cookies)
    monkeypatch.delenv("EVO_BROWSER_CDP", raising=False)
    with pytest.raises(WorkerStateError, match="EVO_BROWSER_CDP"):
        ran(STATE, Run(home, types.SimpleNamespace(out="state.json")))
    monkeypatch.setenv("EVO_BROWSER_CDP", "http://127.0.0.1:9333")
    assert ran(STATE, Run(home, types.SimpleNamespace(out="state.json"))) == 0
    assert seen == ["http://127.0.0.1:9333"]
    written = json.loads((home.run_dir(RUN) / "state.json").read_text(encoding="utf-8"))
    assert written["cookies"][0]["name"] == "sid"
    assert "1 cookies" in capsys.readouterr().out
    with pytest.raises(WorkerStateError, match="worktree"):
        ran(STATE, Run(home, types.SimpleNamespace(out=str(home.worktrees_dir / "demo-7" / "state.json"))))


def test_browser_login_command_notes_the_decision_for_the_daemon_and_a_step_run_just_ends(home, monkeypatch, capsys):
    replies = [
        {"outcome": "asked", "decision_id": 41, "identity_id": 3, "message": "open"},
        {"outcome": "failed", "decision_id": None, "identity_id": 3, "message": "the run fails: log the identity in"},
    ]
    sent = []

    class Hub:
        async def browser_login(self, run_id, body):
            sent.append((run_id, body))
            return replies.pop(0)

    args = types.SimpleNamespace(
        identity="northwind-admin", url="https://northwind.example/login", reason="  it  expired "
    )
    assert ran(LOGIN, Run(home, args, Hub())) == 0
    assert sent[0] == (RUN, {"identity": "northwind-admin", "url": args.url, "reason": "it expired"})
    assert logins.read(home, RUN) == {41: 3}
    assert json.loads(home.decisions_path(RUN).read_text(encoding="utf-8").splitlines()[-1]) == {"id": 41}
    assert "End your turn" in capsys.readouterr().out
    assert ran(LOGIN, Run(home, args, Hub())) == 0
    assert "ends" in capsys.readouterr().out and logins.read(home, RUN) == {41: 3}  # nothing noted for the failure
    with pytest.raises(WorkerStateError, match="http or https"):
        ran(LOGIN, Run(home, types.SimpleNamespace(identity="x", url="ftp://x", reason="r"), Hub()))


def test_browser_run_commands_serve_every_kind_of_run_but_a_browser_login():
    assert browser_login.KIND not in BROWSER_KINDS
    assert LOGIN.kinds == STATE.kinds == BROWSER_KINDS


def test_login_decision_answer_is_read_from_the_message_that_hands_it_to_the_agent(home):
    logins.note(home, RUN, 41, 3, "https://northwind.example/login")
    logins.note(home, RUN, 42, 3, "https://northwind.example/login")
    home.browser_logins_path(RUN).write_text(
        home.browser_logins_path(RUN).read_text(encoding="utf-8") + "not json\n", encoding="utf-8"
    )
    asked = logins.read(home, RUN)
    assert asked == {41: 3, 42: 3}
    messages = [answer(41, "done"), answer(42, "skip"), answer(99, "done"), answer(41, None), {"text": "hello"}]
    assert logins.chosen(messages, asked) == [(41, "done"), (42, "skip"), (41, None)]
    assert browser_login.chosen_option("only one line") is None
    assert logins.read(WorkerHome(home.root / "other"), RUN) == {}


def test_login_decision_done_saves_the_profile_and_restarts_chromium_and_skip_saves_nothing(home):
    notes = []
    checkpoints = []

    class Browser:
        cdp = "http://127.0.0.1:9333"

        def __init__(self, saved=True, fails=False):
            self.saved, self.fails = saved, fails
            self.profiles = types.SimpleNamespace(take_notes=lambda: ["profile note"])

        async def checkpoint(self):
            checkpoints.append(1)
            if self.fails:
                raise RuntimeError("chromium did not start")
            return self.saved

    def agent(browser):
        daemon = types.SimpleNamespace(home=home)
        run = types.SimpleNamespace(id=RUN, daemon=daemon, note=notes.append)
        return types.SimpleNamespace(run=run, browser=browser, logins_seen=set())

    logins.note(home, RUN, 41, 3, "https://northwind.example/login")
    logins.note(home, RUN, 42, 3, "https://northwind.example/login")
    me = agent(Browser())
    asyncio.run(Agent.login_answered(me, [answer(42, "skip"), answer(41, "done"), answer(41, "done")]))
    assert checkpoints == [1], "Skip saves nothing; a decision answered twice saves once"
    assert any("#42" in line and "no profile is saved" in line for line in notes)
    assert any("#41" in line and "saved to the identity" in line for line in notes)

    notes.clear()
    asyncio.run(Agent.login_answered(agent(Browser(saved=False)), [answer(41, "done")]))
    assert any("was not saved" in line for line in notes)
    notes.clear()
    asyncio.run(Agent.login_answered(agent(Browser(fails=True)), [answer(41, "done")]))
    assert any("did not restart cleanly" in line for line in notes)

    checkpoints.clear()
    asyncio.run(Agent.login_answered(agent(None), [answer(41, "done")]))  # a run without a browser
    asyncio.run(Agent.login_answered(agent(Browser()), [answer(99, "done")]))  # not a login the agent asked
    assert checkpoints == []
