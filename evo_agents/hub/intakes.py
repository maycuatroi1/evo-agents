"""The intake of a project (plan problem-intake, criterion a2): one reading of context that is vague, by a member's
session. The model; the server is ``evo_agents.hub.server.intake``, the command line ``evo_agents.hub.cli_intake``.

An intake has a code ``I-<number>``, given in order within the project, the member who asked for it, the list of
sources the session read (each a kind, a label and a short name, never raw content), and a status: ``open`` until the
session puts its result, then ``done``. The result follows ``evo-intake/1`` (``evo_agents/harness/schemas/
intake.schema.json``): candidate problems, promises and questions; or ``evo-intake/2`` (``intake2.schema.json``), which
adds candidate opportunities (plan opportunity-intake, criterion a3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from evo_agents.hub import problems

STATUSES = ("open", "done")
OPEN, DONE = STATUSES
SCHEMA = "evo-intake/1"  # the format a session puts
SCHEMA_2 = "evo-intake/2"  # the same with candidate opportunities
SCHEMA_FILES = {SCHEMA: "intake", SCHEMA_2: "intake2"}  # format -> name of its file in evo_agents/harness/schemas
SOURCE_LABELS = ("public", "internal", "personal")  # a source labelled personal is counted, never described
MAX_SOURCES = 50
MAX_SOURCE_NAME_CHARS = 200
MAX_NOTE_CHARS = 1000
MAX_CANDIDATES = 50
MAX_PROMISES = 100
MAX_QUESTIONS = 100
MAX_OPPORTUNITIES = 50
DEMAND_SIGNALS = ("external_demand", "pull", "repeated_request", "complaint", "blocker", "workaround")
LEVERAGE_SIGNALS = (
    "reusable_asset",
    "idle_capacity",
)  # a new opportunity needs a line of each kind (or an answers code
# in place of demand), unless a member confirmed it
MIN_INDEPENDENT_SOURCES = 2  # a candidate needs this many sources, or someone who confirmed it
CODE = re.compile(r"I-([1-9][0-9]{0,8})")
REF = r"^I-[1-9][0-9]{0,8}$"  # an intake's code
SOURCE_KINDS = problems.SOURCE_KINDS


def code(number: int) -> str:
    """The code of intake ``number``: I-<number>."""
    return f"I-{number}"


def number_of(ref: str) -> int | None:
    """The number ``ref`` names when it is a code (I-<number>); None otherwise."""
    found = CODE.fullmatch(ref)
    return int(found[1]) if found else None


def source_text(source: Mapping) -> str:
    """How a source reads: ``messenger (internal) Group of the shop``."""
    name = f" {source['name']}" if source.get("name") else ""
    return f"{source['kind']} ({source['label']}){name}"
