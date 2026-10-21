"""The breaker of each repo of the hub's merge queue on GitLab (plan gitlab-merge-queue, criterion a7): when it trips
and what its trip stops. Pure functions over plain values, standard library only; ``evo_agents.hub.server.
merge_breaker`` reads the facts in the job hub.merge_queue, trips the breaker and resets it.

**When it trips** (CAUSES, ``causes``). Each repo of a project has one breaker, armed until one of these holds, the
thresholds being the policy's ``breaker.*`` keys:

- ``uat_check`` and ``prod_check``: a UAT check, a PROD deploy or a PROD check failed again after its retry, or has no
  result (cancelled or skipped, a job the policy names missing from the pipeline, still running after
  ``merge.pipeline_minutes``);
- ``rollback``: the hub rolled PROD back;
- ``revert``: GitLab would not revert a culprit (a conflict), or the revert's merge request was blocked (its pipeline
  failed, its diff does not undo the merged one);
- ``target_red``: the newest pipeline of the target branch failed, whoever pushed its commit, and no pipeline of the
  branch passed since, for more than ``breaker.target_red_minutes``;
- ``reverts_in_a_row``: ``breaker.reverts_in_a_row`` merges in a row were found the culprit of a failed check, no merge
  between them passing all its checks (``in_a_row``, counted as the Curator's ``failed_in_a_row`` counts a night);
- ``revert_rate``: the share of the merges of the last RATE_DAYS found the culprit is over ``breaker.
  revert_rate_factor`` times the repo's baseline, the share of its first RATE_DAYS in mode auto; before those days are
  over, or when they hold fewer than ``breaker.baseline_min_merges`` merges, over ``breaker.revert_rate_max``
  (``rate_problem``);
- ``judge_misses``: more than ``breaker.judge_misses_7d`` merges the Judge passed were found wrong in MISS_DAYS
  (reverted, rolled back, or sampled faulty).

The first of these a trip names is its cause, what happened before what it counts. A failed check, a rollback or a revert problem trips it when it comes after the last reset. The merges in a row are
those merged since the last reset, and the rate and the Judge's misses trip it only once a merge was found the culprit,
or a pass of the Judge found wrong, since the last reset: a reset is never undone by what happened before it. The
merges of a day never trip it, and the policy has no cap on them (the member's answer 8 of 2026-10-21).

**What it stops.** Tripped, the hub rebases, merges and deploys nothing of the repo: each of its merge requests
``ready`` or in its turn goes ``paused`` (PAUSED_FROM), one at its gates waits there, and no deploy to PROD starts
(a deploy job that failed is not run again). What undoes the failure still runs to its end (the member's answer 3):
the rollback of PROD, the PROD check of a deploy already under way, and the reverts, which go through their gates and
their turn; the hub also goes on following the checks of the merges already merged, to find the culprit. Only an
admin of the project resets it (``evo-agents hub merge resume``): the paused merge requests are ready again, at their
place, and what trips it counts again from then.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evo_agents.hub import curator

STATES = ARMED, TRIPPED = ("armed", "tripped")
# What trips it, what happened before what it counts: the first a trip names is its cause.
CAUSES = (
    "uat_check",
    "prod_check",
    "rollback",
    "revert",
    "target_red",
    "reverts_in_a_row",
    "revert_rate",
    "judge_misses",
)
CAUSE_WORDS = {  # as the title of the notice names them
    "uat_check": "a UAT check failed",
    "prod_check": "a PROD deploy or check failed",
    "rollback": "PROD was rolled back",
    "revert": "the revert of a failure cannot go on",
    "target_red": "its target branch stayed red",
    "reverts_in_a_row": "merges were reverted one after the other",
    "revert_rate": "too many of its merges were reverted",
    "judge_misses": "the Judge passed too many merges found wrong",
}
PAUSED = "paused"  # merge_queue.state of a merge request the breaker holds
PAUSED_FROM = ("ready", "rebasing", "testing")  # a merge request waiting for its turn, or in it before its merge
CULPRIT_STATES = ("reverting", "reverted")  # evo_agents.hub.post_merge.CULPRIT_STATES
FAILED = ("failed", "none")  # a check's results that trip the breaker (post_merge.RESULTS)
RED, GREEN = "failed", frozenset({"success", "manual"})  # a target branch's pipeline (post_merge.PIPELINE_GREEN)
RATE_DAYS = 14  # the window of the revert rate, and the first days in auto its baseline counts
MISS_DAYS = 7  # the window of the Judge's passes found wrong
MAX_REASON_CHARS = 2000  # merge_breakers.reason (merge_gate.MAX_REASON_CHARS)


@dataclass(frozen=True)
class Facts:
    """What the hub knows of a repo when it asks whether its breaker trips."""

    red_minutes: float | None = None  # how long the target branch has been red; None while it is not
    in_a_row: int = 0  # merges found the culprit one after the other, since the last reset (``in_a_row``)
    window: tuple[int, int] = (0, 0)  # (merges, culprits) of the last RATE_DAYS
    baseline: tuple[int, int] | None = None  # (merges, culprits) of the first RATE_DAYS in auto, once they are over
    new_culprit: bool = False  # a merge was found the culprit since the last reset
    misses: int = 0  # merges the Judge passed found wrong in the last MISS_DAYS
    new_miss: bool = False  # one was found wrong since the last reset
    failures: tuple[tuple[str, str], ...] = ()  # (cause, why) of each failed check, rollback or revert problem since


def target_state(status: str | None) -> str | None:
    """``red`` or ``green`` for the newest pipeline of a target branch whose status is ``status``; None while it says
    neither (it runs, it was cancelled or skipped, or there is none), the branch then keeping the state it had."""
    if status == RED:
        return "red"
    return "green" if status in GREEN else None


def in_a_row(merges: Iterable[tuple[str, bool, str | None]]) -> int:
    """How many of ``merges`` (state, whether its UAT check passed or a later one cleared it, its PROD result), in merge
    order, were found the culprit one after the other at the end, no merge between them passing all its checks; a
    merge still followed, or ended without every check passed, neither counts nor breaks the run."""
    found = []
    for state, uat_passed, prod in merges:
        if state in CULPRIT_STATES:
            found.append(True)
        elif state == "done" and uat_passed and prod in (None, "passed"):
            found.append(False)
    return curator.failed_in_a_row(found)


def rate_problem(
    window: tuple[int, int], baseline: tuple[int, int] | None, *, factor: float, most: float, min_merges: int
) -> str | None:
    """Why the revert rate of ``window`` (merges, culprits) trips the breaker, against ``baseline`` (the same of the
    repo's first RATE_DAYS in auto, None before they are over); None when it does not."""
    merges, culprits = window
    if merges == 0:
        return None
    rate = culprits / merges
    if baseline is not None and baseline[0] >= min_merges:
        base = baseline[1] / baseline[0]
        limit = factor * base
        named = f"breaker.revert_rate_factor {factor:g} times the repo's baseline {base:.0%}"
    else:
        limit = most
        named = f"breaker.revert_rate_max {most:.0%}, the repo having no baseline yet"
    if rate <= limit:
        return None
    return f"{culprits} of its {merges} merges of the last {RATE_DAYS} days were reverted ({rate:.0%}), over {named}"


def causes(facts: Facts, breaker: Mapping) -> list[tuple[str, str]]:
    """(cause, why) of each thing that trips the breaker of a repo whose facts are ``facts``, under the policy's
    ``breaker`` keys, in the order of CAUSES; empty when nothing does."""
    found: list[tuple[str, str]] = []
    red = breaker["target_red_minutes"]
    if facts.red_minutes is not None and facts.red_minutes > red:
        found.append(("target_red", f"its target branch has been red for {facts.red_minutes:.0f} minutes, over {red}"))
    most = breaker["reverts_in_a_row"]
    if facts.in_a_row >= most:
        found.append(("reverts_in_a_row", f"{facts.in_a_row} merges in a row were reverted (breaker.reverts_in_a_row)"))
    if facts.new_culprit:
        problem = rate_problem(
            facts.window,
            facts.baseline,
            factor=breaker["revert_rate_factor"],
            most=breaker["revert_rate_max"],
            min_merges=breaker["baseline_min_merges"],
        )
        if problem is not None:
            found.append(("revert_rate", problem))
    allowed = breaker["judge_misses_7d"]
    if facts.new_miss and facts.misses > allowed:
        why = f"the Judge passed {facts.misses} merges found wrong in {MISS_DAYS} days, over {allowed}"
        found.append(("judge_misses", why))
    found.extend(item for item in facts.failures if item[0] in CAUSES)
    rank = {cause: index for index, cause in enumerate(CAUSES)}
    return sorted(found, key=lambda item: rank[item[0]])


def failure_of(kind: str, result: str | None, iid: int | None, why: str | None, *, revert: bool) -> tuple[str, str]:
    """(cause, why) of a line of a merge request's ledger that trips the breaker: a check that failed or has no result
    (``kind`` uat_check, prod_deploy or prod_check, ``result`` failed or none), a rollback, a revert GitLab would not
    make (revert_opened), or a revert's merge request blocked (``revert``)."""
    named = f"!{iid}" if iid is not None else "a merge request"
    said = f": {why}" if why else ""
    if revert:
        return "revert", f"the revert {named} is blocked{said}"
    if kind == "revert_opened":
        return "revert", f"GitLab would not revert {named}{said}"
    if kind == "rollback":
        return "rollback", f"the hub rolled PROD back for {named}{said}"
    check = {"uat_check": "UAT check", "prod_deploy": "PROD deploy", "prod_check": "PROD check"}[kind]
    ended = "failed" if result == "failed" else "has no result"
    return ("uat_check" if kind == "uat_check" else "prod_check"), f"the {check} of {named} {ended}{said}"


def reason_of(found: list[tuple[str, str]]) -> str:
    """The reason a breaker tripped, from ``found`` (``causes``), on one line of at most MAX_REASON_CHARS."""
    seen: list[str] = []
    for _, why in found:
        line = " ".join(why.split())
        if line not in seen:
            seen.append(line)
    text = "; ".join(seen)
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 3] + "..."
