"""The logins a run's agent asked a person to do (plan agent-browser, criterion a8): ``evo-agents worker browser login``
notes each in ``runs/<run>/browser-logins.jsonl`` (``WorkerHome.browser_logins_path``), one JSON object a line with the
decision's id and the identity's, as ``worker ask`` notes a decision.

The daemon reads it when the owner's answer reaches the agent (``Agent.login_answered``): an answer that chose Done
saves the profile of the run's Chromium to the hub and restarts the browser (``BrowserSession.checkpoint``), and an
answer that chose Skip saves nothing. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

from evo_agents.hub import browser_login

if TYPE_CHECKING:
    from evo_agents.worker.home import WorkerHome


def note(home: WorkerHome, run_id: int, decision_id: int, identity_id: int, url: str) -> None:
    """Note that the agent of run ``run_id`` asked login decision ``decision_id`` for browser identity ``identity_id``."""
    line = json.dumps({"id": decision_id, "identity": identity_id, "url": url})
    with open(home.browser_logins_path(run_id), "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read(home: WorkerHome, run_id: int) -> dict[int, int]:
    """The identity of each login decision the agent asked: ``{decision id: identity id}``."""
    asked: dict[int, int] = {}
    with contextlib.suppress(OSError):
        for line in home.browser_logins_path(run_id).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                found = json.loads(line)
                if isinstance(found["id"], int) and isinstance(found["identity"], int):
                    asked[found["id"]] = found["identity"]
    return asked


def chosen(messages: list[dict], asked: dict[int, int]) -> list[tuple[int, str | None]]:
    """For each message of the inbox that answers a login decision of ``asked``, the decision's id and the option the
    owner chose (None when they answered in words alone), in the order of the messages."""
    found = []
    for message in messages:
        decision_id = message.get("decision_id")
        if isinstance(decision_id, int) and decision_id in asked:
            found.append((decision_id, browser_login.chosen_option(str(message.get("text") or ""))))
    return found
