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
of a kind other than doc and simulation, not withdrawn, added by a person or confirmed by a writer; never by an agent.
A run no member commanded only proposes: a writer accepts or rejects each proposal, and one made at a rev its claim
is no longer at goes stale.

The routes are under /v1/x/canvas/<project>/ (``server``) for members with a grant on a project that enabled the
plugin: readers read, writers make canvases, edit, undo, add, withdraw and confirm evidence, accept and reject
proposals, ask @strategist for a brainstorm and import, admins give a canvas a label of its own and archive it.
``evo-agents hub canvas list|show|create|edit|undo|evidence|proposal|brainstorm|export|import`` is their client; a
canvas exports as ``evo-canvas/1`` JSON and imports back keeping its claims' codes. The tables are the hub's
(``evo_agents.hub.tables_canvas``, migrations 0067 and 0069), every row of a canvas carrying its label, the project's
by default.

The plugin contributes the agent type ``strategist`` (criterion a5): the profile @strategist with the pack
``canvas/strategist`` (``packs/strategist``: its instructions and the skills lean-canvas and canvas-editing), which a
project's writers mention in a group chat, and whose chat runs go to a worker of the member the project enabled the
type under (``chat_runs: run_as``). Its agent reads canvases and signals, edits claims at the rev it read, undoes a
command, proposes claims and adds evidence with the plugin's tools on the hub's /mcp (``agent``, ``tools``), and reads
the SRS documents with srs__read and srs__search when the project enabled srs. A member's command in chat applies at
once as the member's; a brainstorm is a task run (``brainstorm``) that only proposes.

The hub imports this package for the manifest only: ``content`` holds a claim's content, its checks and diff,
``rules`` the rules of its status, ``store`` reads, ``writes`` keeps, ``proposals`` keeps what is proposed,
``transfer`` exports and imports, ``models`` and ``server`` are the API; ``acting``, ``agent``, ``commands`` and
``brainstorm`` are @strategist's.
"""

from evo_agents.core_plugins.canvas.tools import STRATEGIST_INSTRUCTIONS, TOOLS

SERVER = "evo_agents.core_plugins.canvas.server"
AGENT = "evo_agents.core_plugins.canvas.agent"
STRATEGIST = "strategist"  # acting.STRATEGIST
HANDLERS = {"signals": "find_signals"}  # a tool whose function is named otherwise, beside the module signals

TABLES = (
    ("canvas_canvases", "The Lean Canvas of each product of a project and the label it carries"),
    ("canvas_claims", "Each claim of a canvas as it is now: cell, text, status, confidence, test card, rev"),
    ("canvas_claim_revs", "Every rev of every claim, with its whole content and its provenance"),
    ("canvas_evidence", "The evidence of each claim, only ever added, withdrawn by marking it"),
    ("canvas_proposals", "New claims and changes an agent or the hub proposes, which a writer accepts or rejects"),
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
        "agent_packs": [
            {
                "name": STRATEGIST,
                "title": "Strategist",
                "description": "Brainstorming and keeping a Lean Canvas: assumptions by cell, test cards, the rev "
                "protocol.",
            }
        ],
        "agent_types": [
            {
                "name": STRATEGIST,
                "title": "Strategist",
                "description": "Reads, edits and proposes the claims of the project's Lean Canvases for its writers; "
                "members validate and approve.",
                "mentioned_by": "writer",
                "chat_runs": "run_as",
                "packs": [STRATEGIST],
                "profile": {
                    "handle": STRATEGIST,
                    "name": "Strategist",
                    "description": "Keeps the Lean Canvases: edits claims as members ask, brainstorms assumptions with "
                    "test cards; never validates.",
                    "instructions": STRATEGIST_INSTRUCTIONS,
                },
            }
        ],
        "mcp_tools": [
            {
                "name": name,
                "title": title,
                "description": text,
                "python": f"{AGENT}:{HANDLERS.get(name, name)}",
                "input_schema": schema,
            }
            for name, title, text, schema in TOOLS
        ],
        "routes": [{"name": "canvas", "title": "The canvases of a project", "python": f"{SERVER}:router"}],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
    },
}

__all__ = ["MANIFEST"]
