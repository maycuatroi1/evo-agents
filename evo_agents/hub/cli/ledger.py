"""What ``evo-agents hub problem`` and ``hub opportunity`` share: a project's coded items (P-<n>, O-<n>), each with
revisions and lines of evidence.

:class:`Ledger` is the gateway to one such collection, and the command bases below hold the skeleton of each command;
``cli.problem`` and ``cli.opportunity`` set the noun, the gateway, the arguments and the lines a person reads.
"""

from __future__ import annotations

import argparse
from abc import abstractmethod
from typing import Any, ClassVar

from evo_agents.clikit import Result, Table, Text, UsageError, Views
from evo_agents.hub.cli.base import HubCommand, HubContext, ProjectResource, when

SHOWN_STATEMENT_CHARS = 60  # of a statement in a row of list
METRIC_OPTIONS = {"metric": "key", "aggregate": "aggregate", "window_days": "window_days", "tenant": "tenant"}
NEEDED = ("metric", "aggregate")  # the options a metric cannot go without
REVISION_COLUMNS = ("REVISION", "WRITTEN (UTC)", "BY", "CHANGED")
EVIDENCE_COLUMNS = ("CODE", "SIGNAL", "LABEL", "SOURCE", "SPEAKER", "STATE", "EXCERPT")


def clip(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3] + "..."


def metric(args: argparse.Namespace, held: dict | None) -> dict | None:
    """The metric the options make of ``held`` (the item's now, if any); UsageError when they make none."""
    given = {part: value for option, part in METRIC_OPTIONS.items() if (value := getattr(args, option)) is not None}
    if args.no_metric:
        return None
    if not given:
        return held
    made = {**(held or {}), **given}
    missing = [f"--{option.replace('_', '-')}" for option in NEEDED if METRIC_OPTIONS[option] not in made]
    if missing:
        raise UsageError(f"a metric needs {', '.join(missing)}")
    return made


def revisions_view(lines: list[str], revisions: list[dict]) -> Views:
    """``lines``, then the revisions under ``Revisions:`` when there are any."""
    if not revisions:
        return Views(Text(*lines))
    rows = [
        (entry["revision"], when(entry["created_at"]), entry["created_by"], ", ".join(entry["changed"]) or "-")
        for entry in revisions
    ]
    return Views(Text(*lines, "Revisions:"), Table(REVISION_COLUMNS, rows))


class Ledger(ProjectResource):
    """The coded items of a project: GET and PUT /v1/projects/{p}/<collection> and the routes below it."""

    @abstractmethod
    def code(self, number: int) -> str: ...

    def list(self, **query: Any) -> list[dict]:
        return self.call("GET", **query)

    def get(self, code: str, revision: int | None = None) -> dict:
        return self.call("GET", code, revision=revision)

    def revisions(self, code: str) -> list[dict]:
        return self.call("GET", code, "revisions")

    def next_code(self) -> str:
        return self.code(max((item["number"] for item in self.list()), default=0) + 1)

    def put(self, code: str, body: dict) -> dict:
        return self.call("PUT", code, body=body)

    def close(self, code: str, body: dict) -> dict:
        return self.call("POST", code, "close", body=body)

    def evidence(self, code: str) -> list[dict]:
        return self.call("GET", code, "evidence")

    def add_evidence(self, code: str, body: dict) -> dict:
        return self.call("POST", code, "evidence", body=body)

    def confirm_evidence(self, code: str, line: str) -> dict:
        return self.call("POST", code, "evidence", line, "confirm")


class LedgerCommand(HubCommand):
    """A command on the items of one :class:`Ledger`, named ``noun`` (``a_noun`` with its article)."""

    noun: ClassVar[str]
    a_noun: ClassVar[str]
    ledger: ClassVar[type[Ledger]]

    def of(self, ctx: HubContext) -> Ledger:
        return self.ledger(ctx.hub, ctx.project)  # the hub first, then the project, as the commands always asked


# Reading


class ListItems(LedgerCommand):
    """``list``: the items by rank, those of one ``--status`` alone when given."""

    columns: ClassVar[tuple[str, ...]]
    plural: ClassVar[str]

    def query(self, args: argparse.Namespace) -> dict:
        return {"status": args.status or None}

    def run(self, ctx: HubContext) -> Result:
        found = self.of(ctx).list(**self.query(ctx.args))
        status = ctx.args.status
        empty = f"Project {ctx.project} has no {self.plural}{' ' + status if status else ''}."
        return Result(found, Table(self.columns, [self.row(item) for item in found], empty))

    @abstractmethod
    def row(self, item: dict) -> tuple: ...


class ShowItem(LedgerCommand):
    """``show CODE``: an item at its newest revision or at ``--revision``, with its revisions."""

    def run(self, ctx: HubContext) -> Result:
        ledger = self.of(ctx)
        found = ledger.get(ctx.args.code, ctx.args.revision)
        if ctx.args.json:
            return Result(found)
        return Result(found, revisions_view(self.lines(found), ledger.revisions(ctx.args.code)))

    @abstractmethod
    def lines(self, item: dict) -> list[str]: ...

    @staticmethod
    def evidence_line(item: dict) -> list[str]:
        if not item.get("evidence_count"):
            return []
        return [f"Evidence: {item['evidence_count']} lines from {item['source_count']} independent sources"]


# Writing


class SetItem(LedgerCommand):
    """``set [CODE]``: make an item, or write an item's next revision from :meth:`body`."""

    def run(self, ctx: HubContext) -> Result:
        args, ledger = ctx.args, self.of(ctx)
        held, revision = {}, 0
        if args.code is not None:
            held = ledger.get(args.code)
            revision = held["revision"]
        elif args.reopen:
            raise UsageError(f"--reopen opens {self.a_noun} the project has: name its code")
        body = self.body(args, held)
        code = args.code or ledger.next_code()
        body["if_revision"] = revision if args.if_revision is None else args.if_revision
        written = ledger.put(code, body)
        return Result(written, Text(self.said(written, ctx.project)))

    @abstractmethod
    def body(self, args: argparse.Namespace, held: dict) -> dict:
        """The item to write: ``held`` (its fields now, {} for a new one) with the options given; UsageError when
        the options do not make one."""

    def said(self, written: dict, project: str) -> str:
        if not written["changed"]:
            return (
                f"{self.noun.capitalize()} {written['code']} of project {project} is this already, at revision "
                f"{written['revision']}: nothing written."
            )
        made = "Made" if written["revision"] == 1 else "Wrote"
        return f"{made} {self.noun} {written['code']} of project {project}: revision {written['revision']}."


class CloseItem(LedgerCommand):
    """``close CODE --status S --reason TEXT``."""

    def run(self, ctx: HubContext) -> Result:
        args, ledger = ctx.args, self.of(ctx)
        body = {"status": args.status, "reason": args.reason}
        if args.note:
            body["note"] = args.note
        if args.if_revision is not None:
            body["if_revision"] = args.if_revision
        written = ledger.close(args.code, body)
        said = (
            f"Closed {self.noun} {written['code']} of project {ctx.project} ({args.status}): "
            f"revision {written['revision']}."
        )
        return Result(written, Text(said))


# Evidence


class ListEvidence(LedgerCommand):
    """``evidence list CODE``: an item's lines of evidence."""

    def run(self, ctx: HubContext) -> Result:
        found = self.of(ctx).evidence(ctx.args.code)
        empty = f"{self.noun.capitalize()} {ctx.args.code} of project {ctx.project} has no evidence."
        return Result(found, Table(EVIDENCE_COLUMNS, [self.row(line) for line in found], empty))

    @staticmethod
    def row(line: dict) -> tuple:
        return (
            line["code"],
            line["signal"],
            line["label"],
            f"{line['source_kind']} {clip(line['source_uri'], 40)}",
            line["speaker"] or "-",
            "confirmed by " + line["confirmed_by"] if line["confirmed_by"] else "not confirmed",
            clip(line["excerpt"], SHOWN_STATEMENT_CHARS),
        )


class AddEvidence(LedgerCommand):
    """``evidence add CODE``: a line of evidence from a source."""

    def run(self, ctx: HubContext) -> Result:
        args, ledger = ctx.args, self.of(ctx)
        body = {
            "source_kind": args.source_kind,
            "source_uri": args.source_uri,
            "excerpt": args.excerpt,
            "signal": args.signal,
            "label": args.label,
        }
        body |= {name: value for name in ("locator", "speaker") if (value := getattr(args, name)) is not None}
        written = ledger.add_evidence(args.code, body)
        said = f"Added evidence {written['code']} to {self.noun} {written[self.noun]} of project {ctx.project}."
        return Result(written, Text(said))


class ConfirmEvidence(LedgerCommand):
    """``evidence confirm CODE E-<n>``."""

    def run(self, ctx: HubContext) -> Result:
        written = self.of(ctx).confirm_evidence(ctx.args.code, ctx.args.evidence)
        said = f"Confirmed evidence {written['code']} of {self.noun} {written[self.noun]} of project {ctx.project}."
        return Result(written, Text(said))
