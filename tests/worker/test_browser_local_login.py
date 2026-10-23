"""``evo-agents worker browser login NAME`` (plan agent-browser, step 9): a window on this screen, no run."""

import argparse

import pytest

from evo_agents.worker.browser import chromium, cli, local_login
from evo_agents.worker.home import WorkerStateError


def test_browser_cli_headed_flags_drop_headless_and_keep_the_rest(tmp_path):
    headed = chromium.flags(tmp_path, "153.0.1.2", sandbox=True, headed=True)
    plain = chromium.flags(tmp_path, "153.0.1.2", sandbox=True)
    assert "--headless=new" in plain and "--headless=new" not in headed
    assert [flag for flag in plain if flag != "--headless=new"] == headed


def args(**changes) -> argparse.Namespace:
    base = dict(
        name="northwind", project="rollout", owner="member", agent=None, domain=[], identity=None, url=None, reason=None
    )
    return argparse.Namespace(**{**base, **changes})


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"identity": "x"}, "belong to a run"),
        ({"project": None}, "--project"),
        ({"owner": "agent"}, "--agent HANDLE"),
        ({"agent": "scout"}, "--agent HANDLE"),
        ({"url": "ftp://x"}, "--url"),
    ],
)
def test_browser_cli_local_login_refuses_a_bad_combination(changes, said):
    with pytest.raises(WorkerStateError, match=said):
        cli.cmd_local_login(args(**changes))


def test_browser_cli_login_with_nothing_names_no_form():
    with pytest.raises(WorkerStateError, match="NAME"):
        cli.cmd_login(args(name=None))


def test_browser_cli_local_login_needs_a_display_on_linux(monkeypatch):
    monkeypatch.setattr(local_login.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(WorkerStateError, match="no display"):
        local_login.check_screen()
