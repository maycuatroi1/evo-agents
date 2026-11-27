"""The evidence of a problem (plan problem-intake, criterion a1; the model in ``evo_agents.hub.problems``). Table
problem_evidence, schema 0137.

A line of evidence is something a source says that shows the problem hurts: the kind of the source and its address (a
URI or a path), where in it (a message id, a time, a page), who spoke when someone did, an excerpt of at most 300
characters, the signal it shows (complaint, workaround, repeated_request, blocker, commitment, proposed_solution,
contradiction, decision_without_reason), its label (public or internal), who added it, and who confirmed it and when.
The hub refuses a line labelled personal and an excerpt longer than 300 characters with 422: such text stays on the
member's machine. A problem's lines are numbered within it (E-1, E-2, ...).

- GET /v1/projects/{project}/problems/{ref}/evidence: the problem's lines by number, for a member with a grant on the
  project (404 without one, as the problem's own routes answer).
- POST .../evidence: a line added, for a writer of the project; 409 when the problem has a line of the same source,
  place and excerpt already. POST .../evidence/{code}/confirm: the line confirmed by a writer, once (409 after).
- The agent of a run never writes one: @builder proposes a problem with its lines (proposal kind ``problem``, key
  ``evidence``), and the lines are made when a writer approves it (``add_lines``, ``remove_proposed``).

``GET .../problems/{ref}`` counts the lines and the independent sources they come from (a source is a kind and an
address). Writes of one project's problems and evidence wait for each other (the lock of ``problems.write``). Each add,
confirm or removal adds an audit row ``problem.evidence`` naming the problem and the lines, never an excerpt, and
queues a build of the project's knowledge graph, which holds the lines as edges of the problem's node.

The package has four layers, which lint-imports keeps in this order (contract hub-problem-evidence-layers in
pyproject.toml): ``routes`` passes the request's parts and the app's engine to ``service``, which opens the
transaction (``UnitOfWork``), checks and decides, and reads and writes through ``repository`` (``EvidenceStore``,
which the evidence of an opportunity shares); ``models`` holds the request and response models. Other modules import
``service`` and ``models``, or the names below, never ``routes``.
"""

from evo_agents.hub.server.problem_evidence.models import ProblemEvidence, ProblemEvidenceWrite
from evo_agents.hub.server.problem_evidence.service import add_lines, listed, remove_proposed, view, wrong_lines

__all__ = ["ProblemEvidence", "ProblemEvidenceWrite", "add_lines", "listed", "remove_proposed", "view", "wrong_lines"]
