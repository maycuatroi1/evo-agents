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
(``evo_agents.hub.tables_canvas``, migrations 0067 and 0069 to 0072), every row of a canvas carrying its label, the
project's by default. The hub's web shows them on the plugin's native pages (``PAGES``): the canvases in the
project's menu, and under them a canvas's grid of nine cells, a claim's page and a baseline's review.

The plugin contributes the agent type ``strategist`` (criterion a5): the profile @strategist with the pack
``canvas/strategist`` (``packs/strategist``: its instructions and the skills lean-canvas, canvas-editing and
evidence-judging), which a project's writers mention in a group chat, and whose chat runs go to a worker of the member the project enabled the
type under (``chat_runs: run_as``). Its agent reads canvases and signals, edits claims at the rev it read, undoes a
command, proposes claims and adds evidence with the plugin's tools on the hub's /mcp (``agent``, ``tools``), and reads
the SRS documents with srs__read and srs__search when the project enabled srs. A member's command in chat applies at
once as the member's; a brainstorm is a task run (``brainstorm``) that only proposes.

The value check (criterion a4) is the plugin's job ``value_check``, hourly and at once on a batch of the project's
signals (``value_check``): each claim's test card against the signals of the key it names, deterministically, as
evidence the hub writes and proposals of a change of status, never the change itself (``checks``); feedback and
evidence that is no number go before @strategist in a judging run, a task run whose one tool, ``canvas__judge``,
records a verdict with why on each item (``judging``).

A claim links the use cases of the plugin srs that realize it and the signal keys that measure it (criterion a7): a
link an edit adds names a use case of the project's SRS (by code or alias, kept as the SRS's code) or a key of its
registry of signals (``links``). The plugin's source of the knowledge graph (``kg``) gives each canvas as a document
whose claims are Claim nodes (``claim:CL-3``), each implemented by its use cases and measured by its signal keys, so
kg_path goes from a claim to its use cases and the tasks and plan steps that implement them; a mention of ``CL-3``
anywhere in the project links to the claim. A change of a claim queues a build of the project's graph.

Baselines (criterion a6) make a claim's status take effect: the plugin's job ``baseline`` (``schedule``), every 15
minutes, proposes each canvas's baseline once a week at the time of its config's ``baseline`` (16:00 on Friday by
default, in ``baseline.timezone``, else the time zone of the project's Curator charter, else UTC): every change of
status since the status in effect, every status the value check proposes, every test card past its deadline without
real evidence (to extend or retire), and the new claims (``baselines``). The project's writers get the notice
canvas_baseline in their Inbox, which Telegram forwards, and decide each item on the web (``decisions``), never the one
who changed the status, nor a run; once every item has a decision, the baseline is approved with a snapshot that never
changes. A writer may also open a baseline of one claim at once, for another to approve. Every rev emits the event
``canvas.claim``, every approval ``canvas.baseline``, for plugins granted canvas:read and for the hub's events
(``events``).

The hub imports this package for the manifest only: ``content`` holds a claim's content, its checks and diff,
``rules`` the rules of its status, ``links`` checks what a claim links, ``kg`` is the source of the knowledge
graph, ``store`` reads, ``writes`` keeps, ``proposals`` keeps what is proposed,
``transfer`` exports and imports, ``models`` and ``server`` are the API; ``acting``, ``agent``, ``commands`` and
``brainstorm`` are @strategist's; ``value_check``, ``checks`` and ``judging`` are the value check's; ``baselines``,
``decisions``, ``schedule``, ``notices``, ``baseline_models`` and ``events`` are the baselines'.
"""

from evo_agents.core_plugins.canvas.tools import STRATEGIST_INSTRUCTIONS, TOOLS

SERVER = "evo_agents.core_plugins.canvas.server"
AGENT = "evo_agents.core_plugins.canvas.agent"
KG = "evo_agents.core_plugins.canvas.kg"
VALUE_CHECK = "evo_agents.core_plugins.canvas.value_check"
VALUE_CHECK_JOB = "value_check"  # also the job the hub runs on a batch of signals (plugin_jobs.SIGNAL_JOB)
VALUE_CHECK_CRON = "11 * * * *"  # hourly, besides each batch
BASELINE = "evo_agents.core_plugins.canvas.schedule"
BASELINE_JOB = "baseline"
BASELINE_CRON = "*/15 * * * *"  # proposes each canvas's weekly baseline once its time in its zone has come
STRATEGIST = "strategist"  # acting.STRATEGIST
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
BASELINE_DEFAULT = {"weekday": "friday", "time": "16:00"}
TIME_OF_DAY = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"  # evo_agents.hub.curator.TIME_OF_DAY
TIME_ZONE = r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$"  # evo_agents.hub.curator.TIME_ZONE
CONFIG = {
    "type": "object",
    "properties": {
        "baseline": {
            "title": "Weekly baseline",
            "description": "When the hub proposes the baseline of each canvas for the project's writers to approve: a "
            "day of the week and a time of day (16:00 on Friday when left out) in a time zone (the project's Curator "
            "charter's when left out, else UTC).",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "weekday": {"enum": list(WEEKDAYS)},
                "time": {"type": "string", "pattern": TIME_OF_DAY, "description": "HH:MM, 24 hours"},
                "timezone": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": TIME_ZONE,
                    "description": "an IANA time zone, such as Asia/Ho_Chi_Minh",
                },
            },
            "default": BASELINE_DEFAULT,
        }
    },
}
HANDLERS = {"signals": "find_signals"}  # a tool whose function is named otherwise, beside the module signals

TABLES = (
    ("canvas_canvases", "The Lean Canvas of each product of a project and the label it carries"),
    ("canvas_claims", "Each claim of a canvas as it is now: cell, text, status, confidence, test card, rev"),
    ("canvas_claim_revs", "Every rev of every claim, with its whole content and its provenance"),
    ("canvas_evidence", "The evidence of each claim, only ever added, withdrawn by marking it"),
    ("canvas_proposals", "New claims and changes an agent or the hub proposes, which a writer accepts or rejects"),
    ("canvas_judgings", "The items a judging run of @strategist judged for a claim, each verdict and why"),
    ("canvas_baselines", "The baselines of each canvas: proposed, approved with a snapshot never changed, or expired"),
    ("canvas_baseline_items", "What a baseline asks of each claim, and the decision a writer made of it"),
)

# The native pages of the hub's web (web/src/app/(app)/p/[project]/x/canvas, web/src/components/canvas): the canvases,
# with a canvas's grid of nine cells, a claim's page and a baseline's review under it, in the project's menu.
PAGES = {
    "canvases": {"title": "Lean Canvas", "icon": "table", "description": "The living Lean Canvas of each product"},
}

MANIFEST = {
    "id": "canvas",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Lean Canvas",
    "description": "A living Lean Canvas per product: claims with status, test cards, evidence, revs and undo.",
    "engines": {"evo-agents": ">=0.24"},
    "plugin_api": 1,
    "config": CONFIG,
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
        "jobs": [
            {
                "name": VALUE_CHECK_JOB,
                "title": "Value check",
                "description": "Each claim's test card against the project's signals: evidence and proposals, never a "
                "change of status; feedback goes before @strategist to judge. Also at once on a batch of signals.",
                "python": f"{VALUE_CHECK}:run",
                "cron": VALUE_CHECK_CRON,
            },
            {
                "name": BASELINE_JOB,
                "title": "Weekly baseline",
                "description": "Proposes each canvas's baseline once a week at the time of the config's baseline, for "
                "the project's writers to approve on the web: changes of status, the value check's proposals, test "
                "cards past their deadline.",
                "python": f"{BASELINE}:run",
                "cron": BASELINE_CRON,
            },
        ],
        "routes": [{"name": "canvas", "title": "The canvases of a project", "python": f"{SERVER}:router"}],
        "kg_sources": [
            {"name": "canvases", "title": "The canvases of a project and their claims", "python": f"{KG}:kg_source"}
        ],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
        "pages": [{"name": name, **page} for name, page in PAGES.items()],
    },
}

__all__ = ["MANIFEST"]
