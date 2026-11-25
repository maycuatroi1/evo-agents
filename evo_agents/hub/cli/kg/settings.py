"""``evo-agents hub kg settings``: show, set or undo the KG settings of a hub project (``evo_agents.kg.settings``).

``--set KEY=VALUE`` (repeatable) puts keys on top of what the project has, with ``--reason`` and ``--figures`` (a json
object) kept in its change list; ``--undo`` puts back the newest change that is not undone. A key that acts on a build
waits for the next build and is named in ``needs_rebuild``. Standard library only.

:data:`SETTINGS` and :data:`MEASURE` are class commands of ``evo_agents.clikit``: :class:`KgSettings` and
:class:`KgMeasure` are the gateways to their routes, :class:`SettingsChange` the value object that turns the options
of ``settings`` into the body of a change.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from evo_agents.clikit import Arg, Result, Text, UsageError, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg import MEASURE_TIMEOUT
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource
from evo_agents.kg import settings as kg_settings

MEASURE_KEYS = (
    "build_id",
    "split",
    "questions",
    "current",
    "candidate",
    "by_type",
    "candidate_settings",
    "needs_rebuild",
    "why",
)
CHANGES_SHOWN = 5  # the newest changes the text lists


class KgSettings(KgResource):
    """The KG settings of a project: GET and PUT /v1/kg/{p}/settings, POST .../settings/undo."""

    collection = "settings"

    def get(self) -> dict:
        return self.call("GET")

    def put(self, body: dict) -> dict:
        return self.call("PUT", body=body)

    def undo(self, reason: str | None) -> dict:
        return self.call("POST", "undo", body={"reason": reason})


class KgMeasure(KgResource):
    """A candidate of a project's KG settings scored on its question set: POST /v1/kg/{p}/measure."""

    collection = "measure"

    def score(self, body: dict) -> dict:
        return self.call("POST", body=body, timeout=MEASURE_TIMEOUT)


def _values(items: list[str]) -> dict:
    changes = {}
    for item in items:
        key, sep, text = item.partition("=")
        if not sep:
            raise kg_settings.SettingsError(f"--set {item!r}: write KEY=VALUE")
        changes[key.strip()] = kg_settings.parse_value(key.strip(), text)
    return changes


def _figures(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        found = json.loads(text)
    except ValueError:
        raise ValueError("--figures must be a json object") from None
    if not isinstance(found, dict):
        raise ValueError("--figures must be a json object")
    return found


def kg_strategy_error():
    from evo_agents.kg import strategy

    return strategy.StrategyError


@dataclass(frozen=True)
class SettingsChange:
    """What ``--set`` and ``--strategy`` put on top of a project's settings, with the reason and figures kept."""

    values: dict
    reason: str | None
    figures: dict | None

    @classmethod
    def of(cls, args: argparse.Namespace) -> SettingsChange:
        """Raises SettingsError, ValueError or StrategyError for options that say no change the hub takes."""
        values = {}
        if args.strategy:  # the strategy's settings and its name: the hub keeps the strategy as a key
            from evo_agents.kg import strategy as kg_strategy

            chosen = kg_strategy.find(args.strategy)
            values = {**chosen.settings, "strategy": chosen.name}
        values.update(_values(args.set or []))
        return cls(values, args.reason, _figures(args.figures))

    def body(self) -> dict:
        return {"values": self.values, "reason": self.reason, "figures": self.figures}


def settings_json(project: str, found: dict) -> dict:
    """What ``--json`` prints, from the hub's answer."""
    return {
        "project": project,
        "settings": found["values"],
        "overrides": found["overrides"],
        "needs_rebuild": found["needs_rebuild"],
        "changes": found["changes"],
    }


def settings_lines(out: dict) -> list[str]:
    """What a person reads of :func:`settings_json`: the values, what waits for a build, the newest changes."""
    lines = [
        f"{key} = {value}" + ("  (set)" if key in out["overrides"] else "") for key, value in out["settings"].items()
    ]
    if out["needs_rebuild"]:
        lines.append(f"takes effect with the next build: {', '.join(out['needs_rebuild'])}")
    for change in out["changes"][:CHANGES_SHOWN]:
        who = change["changed_by"] or (f"run {change['run_id']}" if change["run_id"] else "-")
        lines.append(f"change {change['id']} ({change['kind']}, {who}): {change['reason'] or 'no reason given'}")
    return lines


class KgSettingsCommand(HubCommand):
    name = "settings"
    help = "show, set (--set KEY=VALUE) or undo (--undo) the KG settings of a hub project"
    arguments = (
        KG_PROJECT,
        Arg("--set", action="append", metavar="KEY=VALUE", help="a key and its new value; repeatable"),
        Arg(
            "--strategy",
            help="set a strategy: its name (built-in) or file, with the settings it carries (docs/kg-strategies.md)",
        ),
        Arg("--undo", action="store_true", help="put back the newest change that is not undone"),
        Arg("--reason", help="why, kept with the change"),
        Arg("--figures", help="a json object of the numbers the change rests on"),
    )
    output = returns_object("project", "settings", "overrides", "needs_rebuild", "changes")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        settings = KgSettings.of(ctx)
        try:
            if args.undo:
                found = settings.undo(args.reason)
            elif args.set or args.strategy:
                found = settings.put(SettingsChange.of(args).body())
            else:
                found = settings.get()
        except (kg_settings.SettingsError, ValueError, kg_strategy_error()) as exc:
            raise UsageError(str(exc)) from None
        out = settings_json(ctx.project, found)
        return Result(out, Text(*settings_lines(out)))


def measure_json(project: str, found: dict) -> dict:
    """What ``hub kg measure --json`` prints, from the hub's answer."""
    return {"project": project, **{k: found.get(k) for k in MEASURE_KEYS}}


def measure_lines(out: dict) -> list[str]:
    """What a person reads of :func:`measure_json`."""
    if out["why"]:
        lines = [f"nothing measured: {out['why']}"]
    else:
        lines = [
            f"{out['questions']} {out['split']} questions on build {out['build_id']}",
            f"current:   {out['current']}",
            f"candidate: {out['candidate']}",
        ]
        lines.extend(type_lines(out["by_type"]))
    if out["needs_rebuild"]:
        lines.append(f"not measured, they need a build: {', '.join(out['needs_rebuild'])}")
    return lines


def type_lines(by_type: dict | None) -> list[str]:
    """One line per question type with its hit@5 under both settings, ``LOWER`` when the candidate's falls."""
    lines = []
    for kind, sides in sorted((by_type or {}).items()):
        cur, cand = (sides.get("current") or {}), (sides.get("candidate") or {})
        cur_hit, cand_hit = cur.get("hit@5"), cand.get("hit@5")
        lower = cur_hit is not None and cand_hit is not None and cand_hit < cur_hit
        lines.append(f"  {kind}: hit@5 current {cur_hit}, candidate {cand_hit}" + ("  LOWER" if lower else ""))
    return lines


def candidate_values(path: str | None) -> dict:
    """The keys and values of the json file ``path`` (none without one); OSError or ValueError when it holds no
    json object."""
    values = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    if not isinstance(values, dict):
        raise ValueError("the settings file must hold a json object")
    return values


class KgMeasureCommand(HubCommand):
    name = "measure"
    help = "score a candidate of the KG settings on the project's question set, changing nothing"
    parser_options = {
        "description": "Score a candidate of the KG settings on the project's question set, changing nothing. The hub "
        f"opens the graph and asks the questions twice, so this waits up to {MEASURE_TIMEOUT:.0f} s for its answer."
    }
    arguments = (
        KG_PROJECT,
        Arg("--settings", help="a json file of keys and values, tried on top of the project's settings"),
        Arg("--split", choices=("tune", "holdout"), default="tune", help="the part of the set asked"),
        Arg("--max-questions", type=int, default=300, help="at most this many questions"),
    )
    output = returns_object("project", *MEASURE_KEYS)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        measure = KgMeasure.of(ctx)
        try:
            values = candidate_values(args.settings)
        except (OSError, ValueError) as exc:
            raise UsageError(str(exc)) from None
        out = measure_json(
            ctx.project, measure.score({"values": values, "split": args.split, "max_questions": args.max_questions})
        )
        return Result(out, Text(*measure_lines(out)))


SETTINGS = KgSettingsCommand()
MEASURE = KgMeasureCommand()
NODES = (SETTINGS, MEASURE)
