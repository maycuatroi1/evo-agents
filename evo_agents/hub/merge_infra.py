"""Whether a merge check failed for a reason of the hub's own machinery rather than of the pull request it judges
(``evo_agents.hub.server.merge_holds`` then holds the check for the project's triage instead of handing the failure
to the plan run that asked).

The plan run cannot fix such a failure: a judge run runs the code installed on its worker, not the pull request's.
On 2026-11-05 eight plan runs of one pull request parked on its merge check one after the other for three hours, each
woken with a failure of the hub: a git without ``merge-tree --write-tree`` on a judge's node, the scanner of the
diff's signs, a flaky test of the base, the judge watchdog; one asked its owner whether to merge. ``CAUSES`` are what
the hub reads as its own:

- ``no_verdict``: JUDGE_ATTEMPTS judge runs in a row ended without a verdict;
- ``judge_setup``: so did they, the last one failing before its agent ran (``SETUP_CAUSES``);
- ``judge_silent``: the Judge's agent ended without the verdict its last message must hold;
- ``base_fails``: every check that failed also fails on the base branch alone, where the worker ran it again
  (``base_exit_code`` of a verify or hidden result);
- ``no_security``: the security pass gave no result.

A failed verdict has these causes only when nothing of the pull request fails it too (``verdict_causes``): the
verdict would pass with each of them taken away. A conflict with the base, a check that fails on the merge result
alone, a dimension the Judge failed, a finding left open, another head read, and a sign nobody weighed are the pull
request's, and the failure goes back to the plan run as before.

A hold lasts HOLD_HOURS at most: the hub then judges the check again by itself. A check is held TRIAGE_ROUNDS times
at one head at most; after that its failure goes back to the plan run, saying it was the hub's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from evo_agents.hub import runs

CAUSES = NO_VERDICT, JUDGE_SETUP, JUDGE_SILENT, BASE_FAILS, NO_SECURITY = (
    "no_verdict",
    "judge_setup",
    "judge_silent",
    "base_fails",
    "no_security",
)
SETUP_CAUSES = (*runs.PREFLIGHT_CAUSES, "checkout")  # a judge run that failed with one never started its agent
TRIAGE_ROUNDS = 3  # holds of one check at one head; the next failure of the hub goes back to the plan run
HOLD_HOURS = 6  # a hold nobody ended by then is judged again by the hub
SAID = {
    NO_VERDICT: "its judge runs ended without a verdict",
    JUDGE_SETUP: "its last judge run failed before its agent ran",
    JUDGE_SILENT: "the Judge's agent ended without a verdict",
    BASE_FAILS: "every check that failed also fails on the base branch alone",
    NO_SECURITY: "the security pass gave no result",
}


def no_verdict_cause(failure_cause: str | None) -> str:
    """The cause of a check whose judge runs all ended without a verdict, the last one with ``failure_cause``."""
    return JUDGE_SETUP if failure_cause in SETUP_CAUSES else NO_VERDICT


def base_failed(item: Mapping) -> bool:
    """Whether the check of result ``item`` failed, and failed on the base branch alone too."""
    base = item.get("base_exit_code")
    return item.get("exit_code") != 0 and isinstance(base, int) and not isinstance(base, bool) and base != 0


def without_base(items: Iterable[Mapping]) -> list[dict]:
    """``items`` with each check that fails on the base branch alone too taken as passed."""
    return [{**item, "exit_code": 0} if base_failed(item) else dict(item) for item in items]


def verdict_causes(
    *,
    passed: bool,
    unweighed_signs: bool,
    head_matches: bool,
    checks_ok: bool,
    checks_ok_without_base: bool,
    agent: str | None,
    judge_ok: bool,
    judge_ok_with_security: bool,
) -> list[str]:
    """The hub's own causes of a failed verdict; none when it passed or when the pull request fails it too.
    ``checks_ok_without_base``: the checks pass with each one that fails on the base too taken as passed;
    ``judge_ok_with_security``: the Judge's round passes with a security pass that gave no result taken as passed."""
    if passed or unweighed_signs or not head_matches:
        return []
    causes = []
    if not checks_ok:
        if not checks_ok_without_base:
            return []
        causes.append(BASE_FAILS)
    if not judge_ok:
        if agent is None:
            causes.append(JUDGE_SILENT)
        elif judge_ok_with_security:
            causes.append(NO_SECURITY)
        else:
            return []
    return causes


def said(causes: Iterable[str]) -> str:
    """The causes in words, for a reason or a thread."""
    return "; ".join(SAID.get(cause, cause) for cause in causes)
