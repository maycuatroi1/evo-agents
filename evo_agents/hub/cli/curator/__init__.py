"""``evo-agents hub curator``: the night shift of a project, as its charter sets it (``evo_agents.hub.curator``).

``curator charter show`` prints the charter at its newest revision, or ``--revision N`` (GET
/v1/projects/{p}/curator/charter); ``--json`` prints it whole, with ``planned_tiers``, ``auto_merge``,
``merge_without_ci``, ``answer_by`` and ``judge.runtime`` among its keys.
``curator charter set FILE`` writes a new revision from a YAML or JSON file (``-`` reads stdin), which only an admin of
the project may (PUT .../curator/charter); the keys ``show --json`` adds to the body are left out, so what was shown can
be edited and written back. ``curator charter history`` lists every revision.

``curator status`` shows where the night shift stands: the charter's revision, the schedules, whether they are paused,
and the night now with its runs and cost (GET .../curator). ``curator pause`` pauses every schedule of the project and
cancels the runs they queued that are still queued; within a minute nothing of the project's night shift moves.
``curator resume`` lets them run again. Either takes an admin of the project, or the member a schedule dispatches as.

``curator proposal list`` lists the proposals of the project's review runs, newest first, filtered by ``--state``,
``--tier`` (each repeats), ``--lens`` and ``--run`` (GET .../curator/proposals); ``curator proposal show ID`` shows one
with its tier's reasons, its evidence, its draft plan and the change accepting it made, with why that change waits;
``curator proposal accept|reject|defer ID`` answers it, which only an admin of the project may (POST
.../curator/proposals/{id}/answer), with ``--note`` and, for defer, ``--days``.
Accepting a proposal of a tier the charter's ``planned_tiers`` names (0 to 2 unless it names others, 3 included) makes
it the Curator's plan, which the night shift builds; one of another tier builds nothing. ``curator proposal plan ID``
makes an accepted proposal of such a tier that has no change yet the Curator's plan, as accepting it does now, which
only an admin of the project may (POST .../curator/proposals/{id}/plan); it prints the change. ``curator findings``
lists what the review runs found (GET .../curator/findings), and ``curator figures`` prints the figures of the latest
night, or of ``--night`` (GET .../curator/figures).

``curator changes`` lists what the accepted proposals the charter plans became: the Curator's plan, its branch, its pull
request, the Judge's verdict and the merge, newest first (GET .../curator/changes). ``curator change close ID --reason
TEXT`` closes a change left open with no pull request, such as one whose proposal named no repo, which only an admin of
the project may (POST .../curator/changes/{id}/close); ``curator replay tasks|show``
(``evo_agents.hub.cli.curator.replay``) the replays that measure them, ``curator history status|clusters|sessions|search|lines``
(``evo_agents.hub.cli.curator.history``) the night's history run, the clusters of its facets and the whole history, and
``curator playbook`` and ``curator earlier`` (``evo_agents.hub.cli.curator.playbook``) what the review runs remember, and
``curator probe list|show|add|retire|report`` (``evo_agents.hub.cli.curator.probe``) the probes of proposals and changes. ``curator
finding|propose`` (``evo_agents.hub.cli.curator.session``) record what a member's own session finds and proposes,
as a review run does. ``curator
protection`` lists the project's repos with the last check of their ruleset (GET .../curator/protection), and ``--check
REPO`` checks one now with the Curator's App, which only an admin of the project may (POST
.../curator/protection/{repo}/check): a Builder runs on a repo of GitHub only once its default branch keeps the
Curator's App off.

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library and PyYAML only, like the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), in a module each along
the group's seams: ``charter`` (``curator charter``), ``schedules`` (``status``, ``pause``, ``resume``), ``proposal``,
``findings`` (``findings``, ``figures``) and ``changes`` (``changes``, ``change close``, ``protection``); the gateways
to the hub's routes, and the keys of their answers, are in ``resources``. ``replay``, ``history``, ``playbook``
(``playbook``, ``earlier``), ``probe``, ``session`` (``finding``, ``propose``) and ``evolve`` hold the rest of the
group. :data:`CURATOR` is the group.
"""

from __future__ import annotations

from evo_agents.clikit import Group
from evo_agents.hub.cli.curator.changes import CURATOR_CHANGE, CuratorChanges, CuratorProtection
from evo_agents.hub.cli.curator.charter import CURATOR_CHARTER
from evo_agents.hub.cli.curator.evolve import EVOLVE
from evo_agents.hub.cli.curator.findings import CuratorFigures, CuratorFindings
from evo_agents.hub.cli.curator.history import HISTORY
from evo_agents.hub.cli.curator.playbook import EARLIER, PLAYBOOK
from evo_agents.hub.cli.curator.probe import PROBE
from evo_agents.hub.cli.curator.proposal import CURATOR_PROPOSAL
from evo_agents.hub.cli.curator.replay import REPLAY
from evo_agents.hub.cli.curator.resources import CHANGE_KEYS as CHANGE_KEYS
from evo_agents.hub.cli.curator.resources import PROPOSAL_KEYS as PROPOSAL_KEYS
from evo_agents.hub.cli.curator.schedules import CuratorPause, CuratorResume, CuratorStatus
from evo_agents.hub.cli.curator.session import FINDING, PROPOSE

CURATOR = Group(
    "curator",
    "the night shift of a project: its charter, status, pause, resume, and its review's proposals",
    [
        CURATOR_CHARTER,
        CuratorStatus(),
        CuratorPause(),
        CuratorResume(),
        CURATOR_PROPOSAL,
        CuratorFindings(),
        CuratorFigures(),
        CuratorChanges(),
        CURATOR_CHANGE,
        CuratorProtection(),
        REPLAY,
        HISTORY,
        PLAYBOOK,
        EARLIER,
        PROBE,
        FINDING,
        PROPOSE,
        EVOLVE,
    ],
)
