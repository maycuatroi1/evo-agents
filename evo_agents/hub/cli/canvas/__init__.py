"""``evo-agents hub canvas list|show|create|edit|undo|evidence|proposal|baseline|brainstorm|export|import``: the Lean Canvases
of a project that enabled the core plugin canvas, through its routes under /v1/x/canvas/<project>/
(``docs/plugins.md``, Core plugins). The plugin's code is ``evo_agents.hub.server.canvas``, which this client never
imports: it speaks HTTP only.

``list`` prints the canvases, ``show SLUG`` a canvas by cell with each claim's code, status, confidence and rev, and
``show SLUG --claim CL-3`` a claim whole: its test card, its evidence (each piece real or why not) and every rev with
who wrote it. ``create SLUG --name NAME`` makes a canvas. ``edit SLUG`` changes one claim (``--claim CL-3`` with the
rev it was read at, ``--base-rev``) or makes one (no ``--claim``; ``--cell`` and ``--text``), or applies ``--file``,
JSON ``{"changes": [...], "reason": "..."}`` as the route takes it, all or nothing. A claim changed since the rev it was
read at refuses the edit: the error lists, per claim, the rev it is at, who changed it and what differs.
``--use-cases``, ``--signal-keys``, ``--problems`` and ``--opportunities`` give the whole list of what the claim links, comma-separated.
``undo SLUG CL-3`` takes back the claim's current rev (``--rev`` names it; read from the hub when left out). ``evidence
add|withdraw|confirm`` keeps a claim's evidence. ``proposal list SLUG`` prints what an agent or the hub proposed (the
open ones unless ``--status`` names others), ``proposal accept SLUG ID`` writes one as the caller's rev and ``proposal
reject SLUG ID`` closes it. ``baseline list SLUG`` prints the baselines, ``baseline show SLUG N`` one with its items
and what the caller may decide of each, ``baseline open SLUG CL-3`` opens one of a claim at once and ``baseline decide
SLUG N CL-3 accepted|rejected|deferred|extended|retired`` decides an item (a member does; the hub refuses a run).
``brainstorm SLUG`` hands @strategist a brainstorm, a task run that only proposes.
``export SLUG`` prints ``evo-canvas/1`` JSON (``--output`` writes a file) and ``import FILE`` reads it back (``-`` for
stdin), keeping the claims' codes; ``--dry-run`` prints what it would do. ``--json`` prints what the hub answered.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), listed by hand in
:data:`CANVAS`: ``claims`` (list, show, create, edit, undo), ``evidence``, ``proposals`` (proposal, baseline,
brainstorm) and ``files`` (export, import); :class:`~evo_agents.hub.cli.canvas.resources.Canvases` is the gateway to
the plugin's routes. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Group
from evo_agents.hub.cli.canvas.claims import CanvasCreate, CanvasEdit, CanvasList, CanvasShow, CanvasUndo
from evo_agents.hub.cli.canvas.evidence import EVIDENCE
from evo_agents.hub.cli.canvas.files import CanvasExport, CanvasImport
from evo_agents.hub.cli.canvas.proposals import BASELINE, PROPOSAL, Brainstorm

CANVAS = Group(
    "canvas",
    "Lean Canvases of a project: claims, revs, evidence (plugin canvas)",
    [
        CanvasList(),
        CanvasShow(),
        CanvasCreate(),
        CanvasEdit(),
        CanvasUndo(),
        EVIDENCE,
        PROPOSAL,
        BASELINE,
        Brainstorm(),
        CanvasExport(),
        CanvasImport(),
    ],
)
