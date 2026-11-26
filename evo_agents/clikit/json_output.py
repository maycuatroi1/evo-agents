"""What a command prints with ``--json``: the declaration ``evo-agents hub contract`` reads from the parser.

It lives here, under every package of evo_agents, so the command base of ``evo_agents.clikit`` and the commands of
the hub, the worker and the knowledge graph declare it alike; ``evo_agents.hub.contract`` imports it back for the
modules that take it from there.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class JsonOutput:
    """What a command prints with ``--json``.

    ``kind`` is ``object`` (one object with ``keys``), ``array`` (a list of objects with ``keys``) or ``map`` (an
    object whose keys are data, ``keys`` empty). ``schema`` names the OpenAPI component the hub answers with when
    the command prints that answer, and ``added`` the keys the client adds to it; tests check that ``keys`` is
    exactly the component's properties plus ``added``. ``variants`` lists, per option, what is printed instead
    when that option is given.
    """

    kind: str
    keys: tuple[str, ...] = ()
    schema: str | None = None
    added: tuple[str, ...] = ()
    variants: tuple[tuple[str, JsonOutput], ...] = ()

    def to_json(self) -> dict:
        data: dict = {"kind": self.kind, "keys": list(self.keys)}
        if self.schema:
            data["schema"] = self.schema
        if self.variants:
            data["with"] = {option: output.to_json() for option, output in self.variants}
        return data


def returns_object(*keys: str, schema: str | None = None, added: tuple[str, ...] = (), variants=()) -> JsonOutput:
    return JsonOutput("object", keys, schema, added, tuple(variants))


def returns_array(*keys: str, schema: str | None = None) -> JsonOutput:
    return JsonOutput("array", keys, schema)


def returns_map(schema: str | None = None) -> JsonOutput:
    return JsonOutput("map", (), schema)


def json_option(parser: argparse.ArgumentParser, output: JsonOutput, help: str = "machine-readable output") -> None:
    """Add ``--json`` to ``parser`` and declare what it prints."""
    parser.add_argument("--json", action="store_true", help=help)
    parser.set_defaults(json_output=output)
