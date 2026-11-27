"""The proposals of @builder of kind ``problem`` (``proposals``; plan problem-intake, criterion a1): where one
applies, how it is checked, applied and rolled back, as its ``EvidenceKind`` (``records``).

A proposal names a problem of the project (``target.problem``, its code) or none, for a new one whose code the hub
picks when it is proposed and keeps in the target. Its version is the problem's revision, 0 for a new one. It may
carry lines of evidence, written with the problem when a writer approves; a proposal of lines alone names an existing
problem and writes no revision. Rolling back takes the lines it made away, and drops a problem it made.
"""

from __future__ import annotations

from evo_agents.core_plugins.builder.records import EvidenceKind
from evo_agents.hub import problems as model
from evo_agents.hub.server import problem_evidence
from evo_agents.hub.server.problems import service as routes


class ProblemKind(EvidenceKind):
    noun = "problem"
    model = model
    routes = routes
    write_model = routes.ProjectProblemWrite
    lines = problem_evidence
    line_model = problem_evidence.ProblemEvidenceWrite


KIND = ProblemKind()
evidence_of = KIND.evidence_of
