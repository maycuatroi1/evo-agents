"""The proposals of @builder of kind ``opportunity`` (plan opportunity-intake, criterion a1 and a2): where one applies,
how it is checked, applied and rolled back, as its ``KIND`` (``records.EvidenceKind``), which ``proposals`` maps its
kind to.

A proposal names an opportunity of the project (``target.opportunity``, its code) or none, for a new one whose code the
hub picks when it is proposed and keeps in the target. Its version is the opportunity's revision, 0 for a new one. It
may carry lines of evidence, written with the opportunity when a writer approves; a proposal of lines alone names an
existing opportunity and writes no revision. Rolling back takes the lines it made away, and drops an opportunity it made.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from evo_agents.core_plugins.builder.records import EvidenceKind
from evo_agents.hub import opportunities as model
from evo_agents.hub.server import opportunity_evidence as lines_of
from evo_agents.hub.server.opportunities import service as routes
from evo_agents.hub.server.projects import ProjectAccess


def target_problems(target: Mapping) -> list[str]:
    if re.fullmatch(model.REF, str(target.get("opportunity", "O-1"))):
        return []
    return ["target.opportunity is an opportunity's code (O-<number>)"]


class OpportunityKind(EvidenceKind):
    """An opportunity, whose answers of other projects are checked when a writer approves."""

    noun = "opportunity"
    article = "an"
    model = model
    routes = routes
    write_model = routes.ProjectOpportunityWrite
    lines = lines_of
    line_model = lines_of.OpportunityEvidenceWrite
    unchanged_refused = False

    def fields_of(self, row: Any, access: ProjectAccess) -> dict:
        return routes.fields_of(row, access.project_id)

    def dropped(self, row: Any, access: ProjectAccess, proposal_id: int, applied: int) -> BaseModel:
        reason = f"rollback of proposal {proposal_id} of @builder"
        return routes.closing(row, access.project_id, model.DROPPED, reason, None, applied)


KIND = OpportunityKind()
evidence_of = KIND.evidence_of
