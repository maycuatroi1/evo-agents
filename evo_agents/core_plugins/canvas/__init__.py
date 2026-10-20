"""The core plugin canvas: a living Lean Canvas for each product of a project, whose claims carry a status, a test
card, evidence and every rev (criterion a2 of plan living-lean-canvas; ``docs/plugins.md``, Core plugins).

A canvas has the nine cells of a Lean Canvas (problem, customer segments, unique value proposition, solution,
channels, revenue streams, cost structure, key metrics, unfair advantage). A cell holds claims, each with a code unique
in the project and never given again (``CL-3``), a text, a status (assumption, testing, validated, invalidated,
retired), a confidence, a test card (hypothesis, test, metric, threshold and how a value check compares a signal, and
a deadline), evidence (its kind, which way it points, a link, the day it was taken, whether it is synthetic) and the
provenance of each rev (a person, an agent or the hub, the member it was for, the run with its runtime and model, the
chat message that asked for it, and why).

Every change of a claim is a rev. An edit names the rev each claim was read at and is refused whole (409, with both
contents, who changed it and the diff) when one changed since; an undo writes the content of the rev before the
claim's current rev as a new rev. A claim goes to validated only with real evidence that supports it: not synthetic,
of a kind other than doc and simulation, not withdrawn, added by a person or confirmed by a writer.

The routes are under /v1/x/canvas/<project>/ (``server``) for members with a grant on a project that enabled the
plugin: readers read, writers make canvases, edit, undo, add, withdraw and confirm evidence and import, admins give a
canvas a label of its own and archive it. ``evo-agents hub canvas list|show|create|edit|undo|evidence|export|import``
is their client; a canvas exports as ``evo-canvas/1`` JSON and imports back keeping its claims' codes. The tables are
the hub's (``evo_agents.hub.tables_canvas``, migration 0067), every row of a canvas carrying its label, the project's
by default.

The hub imports this package for the manifest only: ``content`` holds a claim's content, its checks and diff,
``rules`` the rules of its status, ``store`` reads, ``writes`` keeps, ``transfer`` exports and imports, ``models``
and ``server`` are the API.
"""

SERVER = "evo_agents.core_plugins.canvas.server"

TABLES = (
    ("canvas_canvases", "The Lean Canvas of each product of a project and the label it carries"),
    ("canvas_claims", "Each claim of a canvas as it is now: cell, text, status, confidence, test card, rev"),
    ("canvas_claim_revs", "Every rev of every claim, with its whole content and its provenance"),
    ("canvas_evidence", "The evidence of each claim, only ever added, withdrawn by marking it"),
)

MANIFEST = {
    "id": "canvas",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Lean Canvas",
    "description": "A living Lean Canvas per product: claims with status, test cards, evidence, revs and undo.",
    "engines": {"evo-agents": ">=0.24"},
    "plugin_api": 1,
    "contributes": {
        "routes": [{"name": "canvas", "title": "The canvases of a project", "python": f"{SERVER}:router"}],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
    },
}

__all__ = ["MANIFEST"]
