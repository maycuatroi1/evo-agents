"""``evo-agents hub curator charter show|set|history``: the charter of a project's night shift.

``show`` prints the charter at its newest revision, or ``--revision N``; ``set FILE`` writes a new revision from a YAML or
JSON file (``-`` reads stdin), the keys ``show --json`` adds left out (:class:`CharterFile`); ``history`` lists every
revision. The words that say what a charter sets (:func:`charter_lines`, :func:`period_of`, :func:`hours_of`) are
shared with ``curator status`` (``evo_agents.hub.cli.curator.schedules``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_array
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, when
from evo_agents.hub.cli.curator.resources import CHARTER, REVISION_KEYS, Charters
from evo_agents.hub.client import HubError
from evo_agents.hub.curator import CHARTER_META, money, period_budget
from evo_agents.hub.history import budget_usd as history_budget
from evo_agents.hub.replay import DEFAULT_TASKS

ON_DUTY = "the pool of the default agent"  # where the night shift's runs go: the hub places them


@dataclass(frozen=True)
class CharterFile:
    """The YAML or JSON file a charter is written from; ``-`` is stdin."""

    source: str

    def body(self) -> dict:
        """The charter in the file, without the keys ``show --json`` adds; HubError when it holds no charter."""
        source = self.source
        try:
            text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise HubError(f"cannot read {source}: {exc.strerror or exc}") from None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise HubError(f"{source} is neither YAML nor JSON: {exc}") from None
        if not isinstance(data, dict):
            raise HubError(f"{source} holds no mapping: a charter is one, with window and night_budget_usd, or pacing")
        return {key: value for key, value in data.items() if key not in CHARTER_META}


def period_of(charter: dict | None) -> str:
    """What the charter's Curator counts in: a night, or with pacing its period (a day or a week)."""
    pacing = (charter or {}).get("pacing")
    return pacing["period"] if pacing else "night"


def hours_of(charter: dict) -> tuple[str, str]:
    """(label, value) of when the Curator runs: its window, or its pacing."""
    pacing = charter.get("pacing")
    if pacing:
        quiet = pacing.get("quiet_hours")
        hours = f"quiet {quiet['start']} to {quiet['end']}" if quiet else "no quiet hours"
        return "pacing", f"{money(pacing['budget_usd'])} a {pacing['period']}, {hours}, {pacing['timezone']}"
    window = charter["window"]  # a charter without pacing has a window
    return "window", f"{window['start']} to {window['end']}, {window['timezone']}"


def _role(role: dict) -> str:
    return role["runtime"] + (f" ({role['model']})" if role.get("model") else "")


def _review(settings: dict) -> str:
    cap = settings.get("budget_usd")
    spend = f"at most {money(cap)}" if cap is not None else "the night's caps"
    trigger = settings.get("trigger")
    if trigger:
        return (
            f"{settings.get('lenses', 3)} lenses, when {trigger['min_new']} new evidence came at least "
            f"{trigger['min_gap_hours']} hours after the previous review, or {trigger['max_gap_days']} days went by; "
            f"figures since the previous review, {spend}"
        )
    return f"{settings.get('lenses', 3)} lenses a night, {settings.get('days', 7)} days of figures, {spend}"


def _replay(settings: dict) -> str:
    cap, tasks = settings.get("budget_usd"), settings.get("tasks", DEFAULT_TASKS)
    if cap is None or not tasks:
        return "none: the charter sets no replay.budget_usd, or replay.tasks 0"
    return f"{tasks} tasks of the holdout split a change, at most {money(cap)} a night"


def _history(charter: dict) -> str:
    cap = history_budget(charter)
    if not cap:
        return "none: history.budget_usd is 0"
    model = (charter.get("history") or {}).get("model") or "the cheapest model of the reviewer's runtime"
    return f"facets of the sessions and runs by {model}, at most {money(cap)} a {period_of(charter)}"


def _settings(charter: dict) -> list[tuple[str, str]]:
    """(label, value) of each thing the charter sets, in the order ``charter show`` prints them."""
    paced = bool(charter.get("pacing"))
    run_cap = (
        money(charter["run_budget_usd"])
        if charter["run_budget_usd"] is not None
        else f"what the {period_of(charter)} has left"
    )
    lines = [
        hours_of(charter),
        ("on duty", f"{ON_DUTY}, runs dispatched as {charter['schedule_owner']}"),
        (
            "period" if paced else "night",
            f"{money(period_budget(charter))}, at most {charter['max_runs_per_night']} runs",
        ),
        ("each run", f"{run_cap}, {charter['run_max_turns']} turns, {charter['run_minutes']} minutes of agent time"),
        ("plans", ", ".join(charter["night_plans"]) or "none"),
        ("decisions", f"at most {charter['max_decisions_per_day']} a day, brief at {charter['brief_at']}"),
        ("planned", ", ".join(f"tier {tier}" for tier in charter.get("planned_tiers", [0, 1, 2])) or "none"),
        ("auto merge", ", ".join(f"tier {tier}" for tier in charter["auto_merge"]) or "none"),
        ("without CI", "merges once the Judge passed" if charter.get("merge_without_ci") else "no: CI must be green"),
        ("answers", "the project's agent, in a thread" if charter.get("answer_by") == "agent" else "the admins"),
        ("protected", ", ".join(charter["protected_paths"]) or "none"),
        ("breaker", f"{charter['circuit_breaker']['max_failed_in_a_row']} failed in a row"),
        ("outcome", f"figures counted again {charter.get('outcome_days', 7)} days after a merge"),
        ("review", _review(charter.get("review") or {})),
        ("replay", _replay(charter.get("replay") or {})),
        ("history", _history(charter)),
        ("reviewer", _role(charter["reviewer"])),
        ("builder", _role(charter["builder"])),
        ("judge", _role(charter["judge"])),
    ]
    checks = charter["judge"].get("hidden_checks")
    lines.append(("hidden checks", "shown to admins only" if checks is None else str(len(checks))))
    lines.append(("git secret", charter.get("git_secret") or "none: no Builder on GitLab"))
    lines.append(("env secrets", ", ".join(charter.get("env_secrets") or []) or "none"))
    read = charter.get("read_git_secrets") or []
    lines.append(("read git", ", ".join(read) + " (review and judge runs)" if read else "none"))
    return lines


def labelled(lines: list[tuple[str, str]]) -> list[str]:
    """(label, value) pairs as lines, the values aligned two spaces after the longest label."""
    width = max(len(label) for label, _ in lines)
    return [f"  {label:<{width}}  {text}" for label, text in lines]


def charter_lines(charter: dict) -> list[str]:
    """The charter as ``charter show`` prints it."""
    head = (
        f"Charter of {charter['project']}, revision {charter['revision']}, by {charter['updated_by']} at "
        f"{when(charter['updated_at'])} UTC"
    )
    goals = [f"  goal {goal['id']}: {goal['what']}" for goal in charter["goals"]]
    return [head, *labelled(_settings(charter)), *goals]


def written_lines(charter: dict) -> list[str]:
    """What ``charter set`` says of the revision it wrote."""
    lines = [f"Charter of {charter['project']} is at revision {charter['revision']}."]
    plans = ", ".join(charter["night_plans"]) or "no plan"
    label, hours = hours_of(charter)
    if label == "pacing":
        lines.append(f"The Curator runs {plans} on {ON_DUTY} as {charter['schedule_owner']}, paced at {hours}.")
    else:
        window = charter["window"]
        lines.append(
            f"The night shift runs {plans} on {ON_DUTY} as {charter['schedule_owner']}, {window['start']} to "
            f"{window['end']} {window['timezone']}."
        )
    return lines


class CharterShow(HubCommand):
    name = "show"
    help = "the charter at its newest revision, or an older one"
    arguments = (PROJECT, Arg("--revision", type=int, metavar="N", help="an older revision"))
    output = CHARTER

    def run(self, ctx: HubContext) -> Result:
        charter = Charters.of(ctx).get(ctx.args.revision)
        return Result(charter, Text(*charter_lines(charter)))


class CharterSet(HubCommand):
    name = "set"
    help = "write a new revision from a YAML or JSON file; an admin of the project only"
    arguments = (Arg("file", metavar="FILE", help="the charter, YAML or JSON; - reads stdin"), PROJECT)
    output = CHARTER

    def run(self, ctx: HubContext) -> Result:
        body = CharterFile(ctx.args.file).body()  # read before signing in, so a bad file touches no hub
        charter = Charters.of(ctx).put(body)
        return Result(charter, Text(*written_lines(charter)))


class CharterHistory(HubCommand):
    name = "history"
    help = "every revision of the charter, newest first"
    arguments = (PROJECT,)
    output = returns_array(*REVISION_KEYS, schema="CharterRevision")

    def run(self, ctx: HubContext) -> Result:
        revisions = Charters.of(ctx).revisions()
        rows = [(row["revision"], row["updated_by"], when(row["updated_at"])) for row in revisions]
        footer = Text(f"{len(rows)} revision(s) of the charter of {ctx.project}")
        return Result(revisions, Views(Table(("REVISION", "BY", "AT (UTC)"), rows), footer))


CURATOR_CHARTER = Group(
    "charter",
    "the project's charter: show it, write a revision, list revisions",
    [CharterShow(), CharterSet(), CharterHistory()],
)
