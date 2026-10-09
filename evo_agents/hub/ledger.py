"""The Curator's ledger as the hub models it: the lines it keeps of each proposal, the figures that set a proposal off,
how the hub judges a merged change once its figures are counted again, and the revert it proposes when they got worse.
Pure functions over JSON values, standard library only, so the api, the hub's worker and the tests share them.
``evo_agents.hub.server.ledger`` holds the table's one writer and its route, ``evo_agents.hub.server.outcomes`` the
job that counts the figures again and the circuit breaker of the night shift.

Each proposal has its lines in the ledger, which the hub only ever adds to: none is changed or deleted. A line says who
acted (ACTORS): ``agent``, an agent of a run of the Curator (the Reviewer that proposed it, the Builder that built it,
the Judge that judged it), with its run; ``user``, a member (who accepted, rejected or deferred it, or merged or closed
its pull request by hand); ``curator``, the hub's own code (the plan it made, the pull request it opened, the signs of
score hacking it found, the merge, the outcome, the revert it proposes). A line names what happened (ACTIONS) and,
when it applies, the commit, the commits before and after on the default branch (``before_sha``, ``after_sha``), the
figures, the Judge's verdict, the pull request and when it merged.

The figures that set a proposal off (``trigger_of``) are taken from the night's figures of the review run that wrote
it (``curator.collect``), as numbers the hub can count again: the figure of the proposal's lens (LENS_METRICS), and the
entries of the figures its evidence and its findings' evidence point at (an environment cause, a cause of failed runs,
a command run again and again, a correction). Each is a metric key that ``read_metric`` reads from any figures.

``outcome_of`` compares them with the same figures counted over the charter's ``outcome_days`` after the merge, per
unit of activity (the sessions and the runs that ran in each span, ``activity``), so spans of different length and
busyness compare: a metric is worse when its rate rose by more than WORSE_FACTOR and it counts at least its
MIN_WORSE, better when it fell by as much. The result (OUTCOMES) is ``revert`` when a metric got worse and none got
better, ``keep`` when none got worse, and ``unclear`` when the figures are mixed, when the proposal named no figure
the hub counts, or when either span had less than MIN_ACTIVITY. A ``revert`` makes the hub propose a revert of the
merge commit (``revert_draft``), a proposal of kind ``revert``, tier 1 at least.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from evo_agents.hub.judge import slug

ACTORS = ("curator", "agent", "user")
ACTIONS = (
    "proposed",  # a review run wrote it (agent), or the hub proposed a revert (curator)
    "dropped",  # it repeats a proposal rejected lately (agent)
    "accepted",  # its owner's answers (user)
    "rejected",
    "deferred",
    "planned",  # it became the Curator's plan (curator)
    "built",  # its Builder ended with every step done (agent)
    "build_failed",  # a Builder of it ended otherwise (agent)
    "pull_opened",  # the hub opened its pull request (curator)
    "judged",  # the Judge's verdict (agent), or the hub's signs of score hacking (curator)
    "merged",  # into the default branch, by the hub (curator) or by hand (user)
    "left_open",  # its change stays open for its owner, with why (curator)
    "closed",  # its pull request was closed without a merge (user)
    "outcome",  # the figures counted again after the merge: keep, revert or unclear (curator)
)
OUTCOMES = ("keep", "revert", "unclear")
DEFAULT_OUTCOME_DAYS = 7  # the charter's outcome_days by default
OUTCOME_DAYS = (1, 90)  # the fewest and most days a charter may wait before it counts a change's figures again
MAX_WHAT_CHARS = 2000
MAX_METRICS = 12  # metrics one proposal's trigger holds
MIN_ACTIVITY = 3  # sessions and runs a span needs before its figures say anything
WORSE_FACTOR = 1.25  # a rate this much higher is worse; this much lower, better
MIN_WORSE = 2  # a count must reach this before it is worse
MIN_WORSE_USD = 0.5  # and a cost this much
MAX_REVERT_EVIDENCE = 20  # evidence of the worse figures a revert proposal names

# lens: the metrics of the night's figures it is about
LENS_METRICS: dict[str, tuple[str, ...]] = {
    "tool_errors": ("tool_errors",),
    "environment": ("environment",),
    "corrections": ("corrections",),
    "failed_runs": ("failed_runs",),
    "tech_debt": ("open_items", "stuck_steps"),
    "cost": ("cost_usd",),
}
METRIC_WORDS = {
    "tool_errors": "tool calls that failed",
    "environment": "failures from the environment",
    "corrections": "turns of a person correcting an agent",
    "failed_runs": "runs that failed or were lost",
    "open_items": "open items of plans",
    "stuck_steps": "steps of active plans that did not move",
    "cost_usd": "the cost of the runs, in USD",
}
PREFIX_WORDS = {
    "environment": "failures from the environment of cause {name}",
    "failed_runs": "runs that failed or were lost of cause {name}",
    "repeated": "times `{name}` ran again and again",
    "tool": "failed calls of tool {name}",
    "program": "failed runs of program {name}",
}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _int(value) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


# Metrics


def metric_words(key: str) -> str:
    """What metric ``key`` counts, in words for the owner."""
    if key in METRIC_WORDS:
        return METRIC_WORDS[key]
    prefix, _, name = key.partition(":")
    template = PREFIX_WORDS.get(prefix)
    return template.format(name=name) if template else key


def activity(figures: Mapping) -> int:
    """The sessions and the runs that ran (done, failed or lost) the figures count: what a metric is a share of."""
    sessions = figures.get("sessions") if isinstance(figures.get("sessions"), Mapping) else {}
    ran = figures.get("runs") if isinstance(figures.get("runs"), Mapping) else {}
    return _int(sessions.get("digests")) + sum(_int(ran.get(state)) for state in ("done", "failed", "lost"))


def _entries(figures: Mapping, key: str, field: str, name: str) -> list[dict]:
    return [item for item in _list(figures.get(key)) if isinstance(item, Mapping) and item.get(field) == name]


def read_metric(figures: Mapping, key: str) -> float:
    """The value of metric ``key`` in ``figures`` (the keys of ``review.FIGURE_KEYS``); 0 for what they do not hold."""
    if not isinstance(figures, Mapping):
        return 0.0
    ran = figures.get("runs") if isinstance(figures.get("runs"), Mapping) else {}
    if key == "tool_errors":
        return float(sum(_int(item.get("errors")) for item in _list(figures.get("tools")) if isinstance(item, Mapping)))
    if key == "environment":
        return float(
            sum(_int(item.get("count")) for item in _list(figures.get("environment")) if isinstance(item, Mapping))
        )
    if key == "failed_runs":
        return float(_int(ran.get("failed")) + _int(ran.get("lost")))
    if key in ("corrections", "open_items", "stuck_steps"):
        return float(len(_list(figures.get(key))))
    if key == "cost_usd":
        return _number(ran.get("cost_usd"))
    prefix, _, name = key.partition(":")
    if prefix == "environment":
        return float(sum(_int(item.get("count")) for item in _entries(figures, "environment", "cause", name)))
    if prefix == "failed_runs":
        found = _entries(figures, "failed_runs", "cause", name)
        return float(sum(_int(item.get("failed")) + _int(item.get("lost")) for item in found))
    if prefix == "repeated":
        return float(sum(_int(item.get("times")) for item in _entries(figures, "repeated_commands", "command", name)))
    if prefix == "tool":
        return float(sum(_int(item.get("errors")) for item in _entries(figures, "tools", "tool", name)))
    if prefix == "program":
        return float(sum(_int(item.get("errors")) for item in _entries(figures, "programs", "program", name)))
    return 0.0


def _run_ids(texts: Iterable[str]) -> set[int]:
    found = set()
    for text in texts:
        kind, _, rest = text.partition(":")
        head = rest.partition(":")[0]
        if kind == "run" and head.isdigit():
            found.add(int(head))
    return found


def evidence_metrics(figures: Mapping, texts: Iterable[str]) -> list[str]:
    """The metric keys of the entries of ``figures`` that the evidence ``texts`` (as ``review.evidence_text`` writes
    them) point at, in the figures' order."""
    texts = set(texts)
    run_ids = _run_ids(texts)
    keys: list[str] = []
    for item in _list(figures.get("environment")):
        if not isinstance(item, Mapping) or not isinstance(item.get("cause"), str):
            continue
        cited = texts & {str(text) for text in _list(item.get("evidence"))}
        if cited or run_ids & {run for run in _list(item.get("runs")) if isinstance(run, int)}:
            keys.append(f"environment:{item['cause']}")
    for item in _list(figures.get("failed_runs")):
        if isinstance(item, Mapping) and isinstance(item.get("cause"), str):
            if run_ids & {run for run in _list(item.get("runs")) if isinstance(run, int)}:
                keys.append(f"failed_runs:{item['cause']}")
    for item in _list(figures.get("repeated_commands")):
        if isinstance(item, Mapping) and isinstance(item.get("command"), str):
            if texts & {str(text) for text in _list(item.get("evidence"))}:
                keys.append(f"repeated:{item['command']}")
    if any(isinstance(item, Mapping) and item.get("evidence") in texts for item in _list(figures.get("corrections"))):
        keys.append("corrections")
    return keys


def trigger_of(figures: Mapping | None, lens: str, texts: Iterable[str], *, night: str | None = None) -> dict:
    """The figures that set off a proposal of ``lens`` whose evidence (its own and its findings') is ``texts``, read
    from the night's ``figures``: {night, since, until, activity, metrics: [{key, what, value}]}. No metric when the
    night has no figures."""
    figures = figures if isinstance(figures, Mapping) else {}
    keys = list(LENS_METRICS.get(lens, ())) if figures else []
    if figures:
        keys += evidence_metrics(figures, texts)
    keys = list(dict.fromkeys(keys))[:MAX_METRICS]
    return {
        "night": night or figures.get("night"),
        "since": figures.get("since"),
        "until": figures.get("until"),
        "activity": activity(figures) if figures else 0,
        "metrics": [{"key": key, "what": metric_words(key), "value": read_metric(figures, key)} for key in keys],
    }


# The outcome


def _rate(value: float, spread: int) -> float:
    return value / spread if spread > 0 else 0.0


def compare(key: str, before: float, before_activity: int, after: float, after_activity: int) -> str:
    """``worse``, ``better`` or ``same``: how metric ``key`` moved, per unit of activity (see the module's
    docstring)."""
    rate_before, rate_after = _rate(before, before_activity), _rate(after, after_activity)
    least = MIN_WORSE_USD if key == "cost_usd" else MIN_WORSE
    if after >= least and rate_after > rate_before * WORSE_FACTOR:
        return "worse"
    if rate_before > 0 and rate_after * WORSE_FACTOR < rate_before:
        return "better"
    return "same"


def outcome_of(trigger: Mapping | None, after: Mapping, *, since: str | None = None, until: str | None = None) -> dict:
    """The outcome of a merged change whose proposal was set off by ``trigger`` (``trigger_of``), from the figures
    ``after`` counted over the span after its merge: {result, reason, since, until, activity_before, activity_after,
    metrics: [{key, what, before, after, before_rate, after_rate, change}]}."""
    trigger = trigger if isinstance(trigger, Mapping) else {}
    before_activity, after_activity = _int(trigger.get("activity")), activity(after)
    metrics = []
    for item in _list(trigger.get("metrics")):
        if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
            continue
        key, value = item["key"], _number(item.get("value"))
        now = read_metric(after, key)
        metrics.append(
            {
                "key": key,
                "what": metric_words(key),
                "before": value,
                "after": now,
                "before_rate": round(_rate(value, before_activity), 4),
                "after_rate": round(_rate(now, after_activity), 4),
                "change": compare(key, value, before_activity, now, after_activity),
            }
        )
    found = {
        "since": since,
        "until": until,
        "activity_before": before_activity,
        "activity_after": after_activity,
        "metrics": metrics,
    }
    changes = {item["change"] for item in metrics}
    if not metrics:
        return {**found, "result": "unclear", "reason": "the proposal names no figure the hub counts"}
    if before_activity < MIN_ACTIVITY or after_activity < MIN_ACTIVITY:
        reason = (
            f"too little activity to compare: {before_activity} sessions and runs before, {after_activity} after, "
            f"at least {MIN_ACTIVITY} each"
        )
        return {**found, "result": "unclear", "reason": reason}
    worse = [item["what"] for item in metrics if item["change"] == "worse"]
    if worse and "better" in changes:
        return {**found, "result": "unclear", "reason": "some figures got better and some worse: " + "; ".join(worse)}
    if worse:
        return {**found, "result": "revert", "reason": "the figures got worse: " + "; ".join(worse)}
    better = [item["what"] for item in metrics if item["change"] == "better"]
    reason = "the figures got better: " + "; ".join(better) if better else "the figures held"
    return {**found, "result": "keep", "reason": reason}


def worse_evidence(after: Mapping, outcome: Mapping) -> list[str]:
    """Evidence, as ``review.parse_evidence`` reads it, that the figures ``after`` hold for the metrics ``outcome``
    found worse: the evidence of an environment cause, a command run again and again or a correction, and the runs of
    a cause of failures; at most MAX_REVERT_EVIDENCE."""
    worse = {item.get("key") for item in _list(outcome.get("metrics")) if item.get("change") == "worse"}
    found: list[str] = []

    def add(texts) -> None:
        for text in texts:
            if isinstance(text, str) and text not in found and len(found) < MAX_REVERT_EVIDENCE:
                found.append(text)

    for item in _list(after.get("environment")):
        if isinstance(item, Mapping) and ({"environment", f"environment:{item.get('cause')}"} & worse):
            add(_list(item.get("evidence")))
    for item in _list(after.get("failed_runs")):
        if isinstance(item, Mapping) and ({"failed_runs", f"failed_runs:{item.get('cause')}"} & worse):
            add(f"run:{run}:1" for run in _list(item.get("runs")) if isinstance(run, int))
    for item in _list(after.get("repeated_commands")):
        if isinstance(item, Mapping) and f"repeated:{item.get('command')}" in worse:
            add(_list(item.get("evidence")))
    if "corrections" in worse:
        add(item.get("evidence") for item in _list(after.get("corrections")) if isinstance(item, Mapping))
    return found


def outcome_trigger(outcome: Mapping, *, night: str | None = None) -> dict:
    """The trigger of a revert proposal: the figures of the outcome that made it, as ``trigger_of`` writes them."""
    return {
        "night": night,
        "since": outcome.get("since"),
        "until": outcome.get("until"),
        "activity": _int(outcome.get("activity_after")),
        "metrics": [
            {"key": item["key"], "what": metric_words(item["key"]), "value": _number(item.get("after"))}
            for item in _list(outcome.get("metrics"))
            if isinstance(item, Mapping) and isinstance(item.get("key"), str)
        ],
    }


# The revert


def revert_verify(merge_sha: str) -> str:
    """The verify of a revert: every file the merge commit changed is as it was on its first parent."""
    return f"git diff --name-only -z {merge_sha}^1 {merge_sha} | xargs -0 git diff --quiet {merge_sha}^1 HEAD --"


def revert_draft(
    *, proposal_id: int, title: str, change_id: int, repo: str, merge_sha: str, pr_url: str | None, reason: str
) -> dict:
    """The draft plan of the hub's proposal to revert merge commit ``merge_sha`` of change ``change_id`` (proposal
    ``proposal_id``) in ``repo``: one outcome step, a ``git revert`` of that commit and nothing else."""
    where = f"pull request {pr_url}" if pr_url else f"commit {merge_sha}"
    return {
        "id": slug(f"revert {proposal_id} {title}", 90),
        "title": f"Revert the Curator's change #{change_id}"[:200],
        "goal": f"The default branch of {repo} no longer holds the change of proposal #{proposal_id} ({where}), "
        "whose figures got worse after it merged.",
        "context": f"The hub counted the figures of proposal #{proposal_id} again after its merge: {reason}.",
        "steps": [
            {
                "id": 1,
                "title": f"Revert {merge_sha[:12]}",
                "repo": repo,
                "what": (
                    f"Revert commit {merge_sha} of {repo} on this plan's branch with `git revert --no-edit -m 1 "
                    f"{merge_sha}` when it is a merge commit (two parents), or `git revert --no-edit {merge_sha}` "
                    "otherwise, and change nothing else. If the revert does not apply cleanly, stop and say which "
                    "files conflict instead of resolving them."
                ),
                "verify": revert_verify(merge_sha),
                "acceptance": [
                    f"every file commit {merge_sha} changed is on the branch as it was before that commit",
                    f"the branch holds a revert of commit {merge_sha} and no other change",
                ],
            }
        ],
    }


def revert_summary(*, proposal_id: int, change_id: int, merge_sha: str, pr_url: str | None, outcome: Mapping) -> str:
    """The summary of a revert proposal: why, and each figure before and after, as markdown."""
    lines = [
        f"The Curator's change #{change_id} of proposal #{proposal_id} merged as {merge_sha}"
        + (f" ({pr_url})" if pr_url else "")
        + f". Counted again from {outcome.get('since')} to {outcome.get('until')}: {outcome.get('reason')}.",
        "",
        "| Figure | Before | After | Per session or run, before | Per session or run, after | Change |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in _list(outcome.get("metrics")):
        if isinstance(item, Mapping):
            what = str(item.get("what"))[:200].replace("|", "\\|")  # a pipe of a command would end the cell
            lines.append(
                f"| {what} | {item.get('before')} | {item.get('after')} | {item.get('before_rate')} | "
                f"{item.get('after_rate')} | {item.get('change')} |"
            )
    lines += [
        "",
        f"Sessions and runs counted: {outcome.get('activity_before')} before, {outcome.get('activity_after')} after.",
    ]
    return "\n".join(lines)
