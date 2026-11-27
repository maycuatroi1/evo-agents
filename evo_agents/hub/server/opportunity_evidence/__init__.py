"""The evidence of an opportunity (plan opportunity-intake, criterion a2; the model in
``evo_agents.hub.opportunities``). Table opportunity_evidence, schema 0143.

A line of evidence is something a source says that shows the opportunity is there, with the fields of a problem's line
(``evo_agents.hub.server.problem_evidence``): the kind of the source and its address, where in it, who spoke, an
excerpt of at most 300 characters, the signal it shows, its label (public or internal), who added it, and who confirmed
it and when. Its signal is one of the eight a problem's line shows (complaint, workaround, repeated_request, blocker,
commitment, proposed_solution, contradiction, decision_without_reason) or one of seven that name an opening: wish,
external_demand, window, reusable_asset, pull, intro, idle_capacity. The hub refuses a line labelled personal and an
excerpt longer than 300 characters with 422: such text stays on the member's machine. An opportunity's lines are
numbered within it (E-1, E-2, ...).

- GET /v1/projects/{project}/opportunities/{ref}/evidence: the opportunity's lines by number, for a member with a
  grant on the project (404 without one, as the opportunity's own routes answer).
- POST .../evidence: a line added, for a writer of the project; 409 when the opportunity has a line of the same
  source, place and excerpt already. POST .../evidence/{code}/confirm: the line confirmed by a writer, once (409 after).
- The agent of a run never writes one: @builder proposes an opportunity with its lines (proposal kind ``opportunity``,
  key ``evidence``), and the lines are made when a writer approves it (``add_lines``, ``remove_proposed``).

``GET .../opportunities/{ref}`` counts the lines and the independent sources they come from. Each add, confirm or
removal adds an audit row ``opportunity.evidence`` naming the opportunity and the lines, never an excerpt, and queues
a build of the project's knowledge graph.

The package has four layers, which lint-imports keeps in this order (contract hub-opportunity-evidence-layers in
pyproject.toml): ``routes`` passes the request's parts and the app's engine to ``service``, which opens the
transaction (``UnitOfWork``), checks and decides, and reads and writes through ``repository`` (the ``EvidenceStore``
of ``evo_agents.hub.server.problem_evidence``); ``models`` holds the request and response models. Other modules import
``service`` and ``models``, or the names below, never ``routes``.
"""

from evo_agents.hub.server.opportunity_evidence.models import OpportunityEvidence, OpportunityEvidenceWrite
from evo_agents.hub.server.opportunity_evidence.service import add_lines, listed, remove_proposed, view, wrong_lines

__all__ = [
    "OpportunityEvidence",
    "OpportunityEvidenceWrite",
    "add_lines",
    "listed",
    "remove_proposed",
    "view",
    "wrong_lines",
]
