"""What the run API says of the browser of a run (plan agent-browser, criterion a3): what a worker's heartbeat reports
of the Chromium its daemon starts (``BrowserReport``, kept in workers.browser), and the help of the browser field of a
dispatch, a run and a claim. A run dispatched with browser goes only to a worker whose heartbeat lists the feature
browser (``runs.BROWSER``), and to such a worker only while it holds fewer browser runs than its report's max. A
dispatch may name browser identities (criterion a4), which imply browser: the run goes to their worker alone, and only
while no other run holds one of them."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub.credentials import MAX_SECRET_NAME_CHARS, SECRET_NAME
from evo_agents.hub.runs import BROWSER_INSTALL, MAX_BROWSER_INSTALL_ERROR, MAX_BROWSERS

CHROMIUM_VERSION = r"^[0-9]{1,4}(\.[0-9]{1,6}){0,3}$"
DISPLAYS = ("headless", "screen")
MAX_RUN_IDENTITIES = 8  # browser identities one run opens with
IdentityName = Annotated[
    str,
    Field(
        max_length=MAX_SECRET_NAME_CHARS,
        pattern=f"^{SECRET_NAME.pattern}$",
        description="a browser identity of yours of that name, else the project's (`evo-agents hub browser identity "
        "list`)",
    ),
]


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


class BrowserInstallRequest(BaseModel):
    """An Install browser of the worker's owner, open until the daemon's heartbeat lists the feature browser or the
    request ends (``evo_agents.hub.server.browser_install``)."""

    requested_at: datetime = Field(description="when it was asked for; it names the request in browser_install")


class BrowserInstallReport(BaseModel):
    """Where the daemon stands with its owner's Install browser (browser_install of the heartbeat answer)."""

    requested_at: datetime = Field(description="the request it is about: requested_at of the answer's browser_install")
    ended: str | None = Field(
        None,
        pattern=r"^[a-z][a-z0-9_]{0,31}$",
        description="null while the daemon installs; failed, the install failed (error says how). The hub ends the "
        "request with it, and with a value it does not know ends nothing",
    )
    error: str | None = Field(
        None, min_length=1, max_length=MAX_BROWSER_INSTALL_ERROR, description="why the install failed"
    )


INSTALL_REQUEST_HELP = (
    "the owner asked the daemon to install Chromium, as `evo-agents worker browser install` does; once installed, its "
    f"heartbeat lists the feature browser. Absent when no request is open; sent only to a daemon that says "
    f"{BROWSER_INSTALL}"
)
INSTALL_REPORT_HELP = (
    f"for a daemon that says {BROWSER_INSTALL}, where it stands with the owner's Install browser the answer carried; "
    "null when it carries none"
)


def no_browser(value) -> bool:
    """Whether a claim leaves its browser out: false, as for every run that did not ask for one."""
    return value is False


def no_identities(value) -> bool:
    """Whether a claim leaves its browser identities out: none, as for every run dispatched without them."""
    return not value


DISPATCH_HELP = (
    "true: the run's agent gets a Chromium of its own, which the worker starts and closes; only a worker whose "
    "heartbeat lists the feature browser takes the run, and the browser counts in the run's slot"
)
RUN_HELP = "a run whose agent got a Chromium of its own on its worker (dispatched with browser); false for any other"
SPEC_HELP = (
    "true: start a Chromium for the run's agent before its first turn and hand its DevTools endpoint to the agent "
    "(EVO_BROWSER_CDP); absent for a run without one"
)
RERUN_HELP = (
    "true: the rerun gets a Chromium of its own; false: it gets none, nor the run's browser identities; null keeps "
    "what the run rerun had"
)
IDENTITIES_HELP = (
    "browser identities the run's Chromium opens with, by name, each yours or the project's and all on one worker: it "
    "starts on the profile of the first, with the cookies of the others, and saves them as the run ends; implies "
    "browser. The run goes only to their worker, pinned to it, and waits in the queue while another run holds one"
)


def identities_field():
    """The field ``identities`` of a dispatch: the names of the browser identities the run opens with."""
    return Field([], max_length=MAX_RUN_IDENTITIES, description=IDENTITIES_HELP)


RUN_IDENTITIES_HELP = "the ids of the browser identities the run's Chromium opened with, in the order dispatched"
SPEC_IDENTITIES_HELP = (
    "the ids of the browser identities to open the run's Chromium with (POST /v1/worker/runs/{id}/browser-profile "
    "hands each one's profile), in order; absent for a run without them"
)
REPORT_HELP = (
    "the Chromium the daemon starts for a browser run, with the feature browser; null for a daemon without one"
)
WORKER_HELP = "what the worker's last heartbeat said of its Chromium; null when it said nothing of one"
