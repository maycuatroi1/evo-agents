"""Login sessions of browser identities (plan agent-browser, criterion a7): a run of kind ``browser`` (``runs.RUN_KINDS``)
in which a person logs an identity in. Pure functions over JSON values, standard library only, so the api and the worker
daemon share them.

A login session has no agent and no repo. The hub queues it on the worker that holds the identity, or, for a Move, on
another worker of the dispatcher's with the feature browser, and the identity is held by the run from its claim
(``browser_holds``). The daemon starts Chromium on the identity's profile, as it is now (an empty one for an identity
never saved, and for a Move: a profile does not leave its worker), opens the login URL, and shows the live view in
human mode from the start. The session lasts at most TIMEOUT_MINUTES.

Save, which the dispatcher asks with ``POST /v1/projects/{p}/runs/{id}/finish``, makes the daemon close Chromium, pack
the profile and send it to the hub, which seals it, raises the identity's version, sets it active and, for a Move, on
the session's worker; the run then ends done and the profile directory on the worker goes. Cancel, and running out of
time, end the run without a save: the identity stays at the version it had.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

KIND = "browser"
TIMEOUT_MINUTES = 30  # a login session's timeout, from the moment its Chromium runs
MAX_ATTEMPTS = 1  # a login session the hub lost is not tried again: the person asks again
MAX_URL_CHARS = 2000
NEEDS_LOGIN_NOTICE = "browser_needs_login"  # runs.NOTICE_KINDS
SCHEMES = ("https", "http")


CATEGORY = "login"  # the category of the decision a run asks (``runs.SHOWN_DECISION_CATEGORIES``)
LOGIN_DONE = "done"  # the option of a login decision that says the person signed in: the worker saves the profile
LOGIN_SKIP = "skip"  # the one that says they will not: nothing is saved
LOGIN_OPTIONS = (
    (LOGIN_DONE, "Done: I signed in", "The browser holds the session now. The profile is saved to the identity."),
    (LOGIN_SKIP, "Skip", "Do not sign in. The agent goes on without it and nothing is saved."),
)
# Why the hub recommends LOGIN_DONE on a login decision (``decision_asks``): the run asked for the login to go on.
LOGIN_REASON = (
    "The run asked for this login to go on with its work; once you sign in, the identity keeps the session for its "
    "later runs too."
)
CHOSEN = re.compile(r"Chosen option: ([A-Za-z0-9][A-Za-z0-9_-]{0,31}),")


def chosen_option(message: str) -> str | None:
    """The key of the option chosen in the inbox message that hands a decision's answer to the agent
    (``server.decisions.answer_message``: its second line); None when the owner answered in words alone."""
    lines = message.split("\n", 2)
    found = CHOSEN.match(lines[1]) if len(lines) > 1 else None
    return found.group(1) if found else None


def title_of(identity: str, *, move: bool = False, worker: str | None = None) -> str:
    """The title of a login session of identity ``identity``."""
    where = f" on {worker}" if move and worker else ""
    return f"{'Move' if move else 'Log in'} browser identity {identity}{where}"[:200]


def default_url(domains: list[str] | tuple[str, ...]) -> str:
    """The page a login session opens when its identity names no login URL: the home page of its first domain."""
    return f"https://{domains[0]}/" if domains else "about:blank"


def valid_url(url: str) -> bool:
    """Whether ``url`` is a page a login session opens: http or https, with a host, no longer than MAX_URL_CHARS and
    without control characters."""
    if not url or len(url) > MAX_URL_CHARS or any(ord(char) < 0x21 or ord(char) == 0x7F for char in url):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme.lower() in SCHEMES and bool(parts.hostname)
    except ValueError:
        return False


def needs_login_title(identity: str) -> str:
    return f"Browser identity {identity} needs a new login"[:200]


LOGIN_QUESTION = "Sign in to {name} for run #{run}?"
STEP_REFUSAL = (
    "run {run} is a {kind} run and cannot wait for a person: browser identity {name} is set to needs_login and the "
    "run fails. Log the identity in on the Browser page of project {project}, then dispatch the run again."
)


def login_context(name: str, url: str, reason: str, project: str, run_id: int) -> str:
    """The markdown a login decision carries: who asks, why, where the page is and where the live view of the run's
    browser is (the run page of the web, its Browser tab)."""
    return (
        f"The agent of run #{run_id} needs browser identity **{name}** signed in.\n\n"
        f"- Why: {reason.strip()}\n"
        f"- Page: {url}\n"
        f"- Live view: [open the browser of run #{run_id}](/p/{project}/runs/{run_id}), take over, sign in there, "
        "then answer Done.\n\n"
        "Done saves the signed-in profile to the identity and the agent goes on. Skip saves nothing."
    )
