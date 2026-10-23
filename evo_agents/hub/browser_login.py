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

from urllib.parse import urlsplit

KIND = "browser"
TIMEOUT_MINUTES = 30  # a login session's timeout, from the moment its Chromium runs
MAX_ATTEMPTS = 1  # a login session the hub lost is not tried again: the person asks again
MAX_URL_CHARS = 2000
NEEDS_LOGIN_NOTICE = "browser_needs_login"  # runs.NOTICE_KINDS
SCHEMES = ("https", "http")


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
