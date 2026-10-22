"""What the run API says of the browser of a run (plan agent-browser, criterion a3): what a worker's heartbeat reports
of the Chromium its daemon starts (``BrowserReport``, kept in workers.browser), and the help of the browser field of a
dispatch, a run and a claim. A run dispatched with browser goes only to a worker whose heartbeat lists the feature
browser (``runs.BROWSER``), and to such a worker only while it holds fewer browser runs than its report's max."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.runs import MAX_BROWSERS

CHROMIUM_VERSION = r"^[0-9]{1,4}(\.[0-9]{1,6}){0,3}$"
DISPLAYS = ("headless", "screen")


class BrowserReport(BaseModel):
    """The Chromium a daemon starts for a browser run, as its heartbeat says."""

    chromium: str = Field(pattern=CHROMIUM_VERSION, description="its version, as `chrome --version` prints it")
    display: Literal[DISPLAYS] = Field(
        description="headless: it runs with --headless=new, no screen needed; screen: it draws on a display"
    )
    max: int = Field(
        ge=1,
        le=MAX_BROWSERS,
        description="how many browser runs the worker holds at once, each its own Chromium; the hub hands it no more",
    )
    sandbox: bool = Field(
        description="whether Chromium runs with its own sandbox; false where the machine does not let it (no "
        "unprivileged user namespaces), as the install found"
    )


def no_browser(value) -> bool:
    """Whether a claim leaves its browser out: false, as for every run that did not ask for one."""
    return value is False


DISPATCH_HELP = (
    "true: the run's agent gets a Chromium of its own, which the worker starts and closes; only a worker whose "
    "heartbeat lists the feature browser takes the run, and the browser counts in the run's slot"
)
RUN_HELP = "a run whose agent got a Chromium of its own on its worker (dispatched with browser); false for any other"
SPEC_HELP = (
    "true: start a Chromium for the run's agent before its first turn and hand its DevTools endpoint to the agent "
    "(EVO_BROWSER_CDP); absent for a run without one"
)
RERUN_HELP = "true: the rerun gets a Chromium of its own; false: it gets none; null keeps what the run rerun had"
REPORT_HELP = (
    "the Chromium the daemon starts for a browser run, with the feature browser; null for a daemon without one"
)
WORKER_HELP = "what the worker's last heartbeat said of its Chromium; null when it said nothing of one"
