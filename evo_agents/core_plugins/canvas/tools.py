"""The MCP tools of the plugin canvas as its manifest declares them (``TOOLS``), and the profile of @strategist, the
agent type that calls them (``STRATEGIST_INSTRUCTIONS``). Plain data with no import, so reading the manifest loads
neither the hub nor SQLAlchemy; the names below are copies of ``evo_agents.hub.tables_canvas`` and
``evo_agents.core_plugins.canvas.content``, which ``tests.hub.test_strategist_agent`` holds them to.

The schemas leave out what @strategist never does: no tool takes the status validated, a new claim takes no status
(it starts as an assumption), and evidence an agent adds is of kind interview, market, doc or simulation only. The hub
checks the same in code (``agent``, ``rules``), so a schema is a help to the agent, never the guard.
"""

CELLS = (  # evo_agents.hub.tables_canvas.CELLS
    "problem",
    "customer_segments",
    "unique_value_proposition",
    "solution",
    "channels",
    "revenue_streams",
    "cost_structure",
    "key_metrics",
    "unfair_advantage",
)
AGENT_STATUSES = ("assumption", "testing", "invalidated", "retired")  # tables_canvas.STATUSES without validated
CONFIDENCES = ("low", "medium", "high")  # tables_canvas.CONFIDENCES
DIRECTIONS = ("supports", "contradicts", "neutral")  # tables_canvas.DIRECTIONS
AGENT_EVIDENCE_KINDS = ("interview", "market", "doc", "simulation")  # what an agent adds, a writer confirms
AGGREGATES = ("last", "mean", "sum", "count", "p50", "p90")  # content.AGGREGATES
COMPARATORS = (">=", ">", "<=", "<")  # content.COMPARATORS
MAX_TEXT_CHARS = 2_000  # tables_canvas.MAX_TEXT_CHARS, and content.MAX_CARD_TEXT_CHARS
MAX_REASON_CHARS = 1_000  # tables_canvas.MAX_REASON_CHARS
MAX_SUMMARY_CHARS = 4_000  # tables_canvas.MAX_SUMMARY_CHARS
MAX_URL_CHARS = 2_000  # tables_canvas.MAX_URL_CHARS
MAX_CHANGES = 100  # writes.MAX_CHANGES
MAX_PROPOSED = 50  # proposals.MAX_PROPOSED
VERDICTS = ("supports", "contradicts", "irrelevant")  # tables_canvas.VERDICTS
MAX_JUDGED = 50  # judging.MAX_ITEMS
MAX_SIGNAL_DAYS = 400  # the hub keeps signals 400 days by default (EVO_HUB_SIGNAL_DAYS)
MAX_LINKS = 20  # content.MAX_LINKS
MAX_CODE_CHARS = 100  # tables_srs.MAX_CODE_CHARS

STRATEGIST_INSTRUCTIONS = (
    "You are @strategist, the agent of this project's Lean Canvases on the evo-agents hub. You read the canvases and "
    "the signals the project measures with canvas__read and canvas__signals, and the SRS documents with srs__read and "
    "srs__search when the project keeps them. In a chat, you change claims as the member who mentioned you asks, with "
    "canvas__edit at the rev you read, and take a command back with canvas__undo; in a task run you only propose, "
    "with canvas__propose; in a judging run the hub hands you, you only judge evidence, with canvas__judge. A claim "
    "you make starts as an assumption with a test card. You never move a claim to "
    "validated and never approve a baseline, even when asked; you mark everything that rests on simulated users as "
    "synthetic. Follow the instructions of your pack."
)

_SLUG = {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,99}$", "description": "the canvas's slug"}
_CODE = {"type": "string", "pattern": "^(?:[Cc][Ll]-)?[1-9][0-9]{0,8}$", "description": "a claim's code, as CL-3"}
_REV = {"type": "integer", "minimum": 1}
_DAY = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "description": "a day, YYYY-MM-DD"}
_TEXT = {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS}
_REASON = {"type": "string", "minLength": 1, "maxLength": MAX_REASON_CHARS, "description": "why, kept with each rev"}
_SIGNAL_KEY = {"type": "string", "pattern": "^[a-z][a-z0-9_]*(\\.[a-z0-9_]+){1,5}$"}
TEST_CARD = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "description": "the whole test card (null takes it away): what would show the claim true, and how a value check "
    "compares a signal with it",
    "properties": {
        "hypothesis": {**_TEXT, "description": "we believe that ..."},
        "test": {**_TEXT, "description": "to verify that, we will ..."},
        "metric": {
            "type": "string",
            "description": "the signal key it measures, as canvas__signals lists it, or feedback",
        },
        "aggregate": {"enum": list(AGGREGATES)},
        "window_days": {"type": "integer", "minimum": 1, "maximum": 90},
        "comparator": {"enum": list(COMPARATORS)},
        "threshold": {"type": "number"},
        "min_samples": {"type": "integer", "minimum": 1},
        "tenant": {"type": "string", "minLength": 1, "maxLength": 100},
        "deadline": _DAY,
    },
}
_CLAIM_FIELDS = {
    "claim": {**_CODE, "description": "the claim it changes, as CL-3; left out: a new claim"},
    "base_rev": {**_REV, "description": "the rev you read the claim at, which canvas__read shows; not for a new one"},
    "cell": {"enum": list(CELLS)},
    "text": _TEXT,
    "status": {"enum": list(AGENT_STATUSES), "description": "not for a new claim, which starts as an assumption"},
    "confidence": {"enum": list(CONFIDENCES)},
    "position": {"type": "integer", "minimum": 0, "description": "its place in the cell, 0 first"},
    "test_card": TEST_CARD,
    "archived": {"type": "boolean", "description": "true archives the claim, false brings it back"},
    "use_cases": {
        "type": "array",
        "maxItems": MAX_LINKS,
        "items": {"type": "string", "minLength": 1, "maxLength": MAX_CODE_CHARS},
        "description": "the codes of the SRS use cases that realize the claim, as srs__search finds them (UC ORD-1): "
        "the whole list, [] for none",
    },
    "signal_keys": {
        "type": "array",
        "maxItems": MAX_LINKS,
        "items": _SIGNAL_KEY,
        "description": "the signal keys that measure the claim, as canvas__signals lists them: the whole list, [] for "
        "none",
    },
}
_CHANGE = {"type": "object", "additionalProperties": False, "properties": _CLAIM_FIELDS}

READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "canvas": {**_SLUG, "description": "the canvas's slug; left out: the canvases of the project"},
        "claim": {**_CODE, "description": "one claim whole: its evidence, revs and open proposals"},
        "archived": {"type": "boolean", "description": "archived claims too"},
    },
}
SIGNALS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "key": {**_SIGNAL_KEY, "description": "one key: its values over time; left out: the registry of keys"},
        "days": {"type": "integer", "minimum": 1, "maximum": MAX_SIGNAL_DAYS, "description": "how far back; 30"},
        "tenant": {"type": "string", "minLength": 1, "maxLength": 100},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "values at most; 50"},
    },
}
EDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canvas", "changes", "reason"],
    "properties": {
        "canvas": _SLUG,
        "changes": {"type": "array", "minItems": 1, "maxItems": MAX_CHANGES, "items": _CHANGE},
        "reason": _REASON,
    },
}
UNDO_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canvas"],
    "properties": {
        "canvas": _SLUG,
        "claims": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_CHANGES,
            "description": "the claims whose current rev to take back; left out: the member's last command in chat",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "rev"],
                "properties": {"claim": _CODE, "rev": {**_REV, "description": "the claim's current rev"}},
            },
        },
        "reason": _REASON,
    },
}
PROPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canvas", "proposals", "reason", "synthetic"],
    "properties": {
        "canvas": _SLUG,
        "proposals": {"type": "array", "minItems": 1, "maxItems": MAX_PROPOSED, "items": _CHANGE},
        "reason": _REASON,
        "synthetic": {"type": "boolean", "description": "true when any of them rests on simulated users or personas"},
    },
}
EVIDENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canvas", "claim", "kind", "direction", "summary", "synthetic"],
    "properties": {
        "canvas": _SLUG,
        "claim": _CODE,
        "kind": {"enum": list(AGENT_EVIDENCE_KINDS)},
        "direction": {"enum": list(DIRECTIONS)},
        "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_CHARS},
        "url": {"type": "string", "minLength": 1, "maxLength": MAX_URL_CHARS},
        "observed_on": {**_DAY, "description": "the day it was taken"},
        "synthetic": {"type": "boolean", "description": "true when it rests on simulated users; a simulation always"},
    },
}

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdicts": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_JUDGED,
            "description": "a verdict on each item you judged; left out: the claims and items of this judging run",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "verdict", "reason"],
                "properties": {
                    "item": {"type": "integer", "minimum": 1, "description": "the item's number canvas__judge shows"},
                    "verdict": {
                        "enum": list(VERDICTS),
                        "description": "whether it supports the claim's hypothesis, contradicts it, or neither",
                    },
                    "reason": {**_REASON, "description": "why, in a sentence or two a member reads before deciding"},
                },
            },
        },
    },
}

TOOLS = (  # name, title, description, input schema
    (
        "read",
        "Read the canvases",
        "Read the Lean Canvases of the project: without canvas, the list; with it, its nine cells with each claim's "
        "code, status, confidence, rev, test card and evidence, and the open proposals; with claim, that claim whole "
        "with its evidence and revs.",
        READ_SCHEMA,
    ),
    (
        "signals",
        "Find signals",
        "The signals the project measures: without key, the registry of keys with the latest value of each; with "
        "key, its values over the last days, newest first. Only what the member you work for may read.",
        SIGNALS_SCHEMA,
    ),
    (
        "edit",
        "Edit claims",
        "Change claims of a canvas, each at the rev you read it at, and make new ones (as assumptions), all or "
        "nothing, as the member who mentioned you asked: written at once as theirs. A claim changed since is refused "
        "with what changed. Only the agent of a chat run of @strategist may call it; never to validated.",
        EDIT_SCHEMA,
    ),
    (
        "undo",
        "Undo a command",
        "Take back the member's last command to you in chat: every claim it changed goes back to the rev before it. "
        "With claims, take back the current rev of each instead. Only the agent of a chat run of @strategist.",
        UNDO_SCHEMA,
    ),
    (
        "propose",
        "Propose claims",
        "Propose new claims (each starts as an assumption, with a test card) or changes of claims at the rev you read "
        "them, for members to accept or reject; nothing changes until one does. Only the agent of a run of "
        "@strategist, or of the Curator's review run on a night of the lens product; never to validated.",
        PROPOSE_SCHEMA,
    ),
    (
        "evidence",
        "Add evidence",
        "Add a piece of evidence to a claim: an interview, a market figure, a document or a simulation, which counts "
        "toward validated only once a writer confirms it, and never when it is synthetic. Only the agent of a run of "
        "@strategist.",
        EVIDENCE_SCHEMA,
    ),
    (
        "judge",
        "Judge evidence",
        "In a judging run the hub handed @strategist: without verdicts, each claim's hypothesis and test card and the "
        "items to judge; with verdicts, whether each item supports the hypothesis, contradicts it or is irrelevant, "
        "with why. Only the agent of a judging run, for the items of its run; it changes no claim.",
        JUDGE_SCHEMA,
    ),
)

__all__ = ["STRATEGIST_INSTRUCTIONS", "TOOLS"]
