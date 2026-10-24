"""What a claim holds at one rev (``Content``), how a change of it is checked, how two revs compare (``diff``), the
claim's code (``CL-<n>``), and the refusal every module of the plugin raises (``CanvasError``).

A rev keeps the whole content of its claim, so an undo writes the content of the rev before it and a diff compares two
contents field by field, the fields of the test card each on its own. A test card holds the four fields of a Test Card
(hypothesis, test, metric, threshold) and how a value check compares a signal with it deterministically (aggregate,
window_days, comparator, min_samples, tenant), with a deadline. Every field of it may be left out; a claim goes to
testing only with a hypothesis, a test and a deadline (``evo_agents.core_plugins.canvas.rules``).

A claim also links the use cases of the plugin srs that realize it, by code (``use_cases``), the keys of the
project's registry of signals that measure it (``signal_keys``), and the problems of the project it answers, by code
P-<number> (``problems``, plan problem-oriented-hub), and the opportunities of the project it links, by code O-<number>
(``opportunities``, plan opportunity-intake): each a set, kept sorted, at most MAX_LINKS. Here they are checked
for their shape alone; ``links`` checks a link a write adds against the project's SRS, registry, problems and opportunities, and names
a use case by its code as the SRS holds it. A rev written before links existed reads as linking nothing.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import date

from evo_agents.hub import opportunities as opportunity_model
from evo_agents.hub import problems as problem_model
from evo_agents.hub.tables_canvas import CELLS, CONFIDENCES, DEFAULT_DELIVERY, DELIVERIES, MAX_TEXT_CHARS, STATUSES
from evo_agents.hub.tables_srs import MAX_CODE_CHARS, code_key

CODE = re.compile(r"^(?:CL-)?([1-9][0-9]{0,8})$", re.IGNORECASE)  # CL-3, cl-3 or 3
METRIC = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,5}$")  # a signal key, as the registry of signals names one
FEEDBACK = "feedback"  # the metric of a test card that counts feedback judged to support the claim
AGGREGATES = ("last", "mean", "sum", "count", "p50", "p90")
COMPARATORS = (">=", ">", "<=", "<")
MAX_CARD_TEXT_CHARS = 2_000  # of a test card's hypothesis and test
MAX_TENANT_CHARS = 100
MAX_SAMPLES = 1_000_000
MAX_WINDOW_DAYS = 90
MAX_LINKS = 20  # use cases a claim links, signal keys, problems, and opportunities
LINKS = (
    "use_cases",
    "signal_keys",
    "problems",
    "opportunities",
)  # the fields of a claim's content that link it, each a sorted tuple
PROBLEM = re.compile(problem_model.REF)  # a problem's code, P-<number>
OPPORTUNITY = re.compile(opportunity_model.REF)  # an opportunity's code, O-<number>
FIELDS = (
    "cell",
    "position",
    "text",
    "status",
    "confidence",
    "delivery",
    "test_card",
    "archived",
    *LINKS,
)  # of a claim's content
CARD_FIELDS = (
    "hypothesis",
    "test",
    "metric",
    "aggregate",
    "window_days",
    "comparator",
    "threshold",
    "min_samples",
    "tenant",
    "deadline",
)


class CanvasError(Exception):
    """Why the hub refuses a request about canvases, with the HTTP status a route answers and any detail (the claims
    of a conflict, the problems of an import); nothing was written."""

    def __init__(self, status: int, message: str, detail: list | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


def code(number: int) -> str:
    """The code a claim is shown and named by: CL-<number>."""
    return f"CL-{number}"


def number_of(ref: str) -> int:
    """The number ``ref`` (CL-3, or 3) names; 422 when it names none."""
    matched = CODE.match(ref.strip())
    if matched is None:
        raise CanvasError(422, f"{ref!r} names no claim: a claim is named CL-<number>, as CL-3")
    return int(matched.group(1))


@dataclass(frozen=True)
class Content:
    """A claim at one rev: its cell, its place in the cell, its text, status, confidence, test card, and whether it is
    archived."""

    cell: str
    position: int
    text: str
    status: str = "assumption"
    confidence: str = "medium"
    delivery: str = DEFAULT_DELIVERY  # code: the build path; action: a task of the plugin tasks (plan problem-intake)
    test_card: dict | None = None
    archived: bool = False
    use_cases: tuple[str, ...] = ()
    signal_keys: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    opportunities: tuple[str, ...] = ()  # plan opportunity-intake

    def as_json(self) -> dict:
        return {**asdict(self), **{name: list(getattr(self, name)) for name in LINKS}}

    @classmethod
    def from_json(cls, value: dict) -> Content:
        found = {name: value[name] for name in FIELDS if name in value}
        return cls(**{**found, **{name: tuple(found[name] or ()) for name in LINKS if name in found}})


def _text(name: str, value, high: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        raise CanvasError(422, f"{name} takes text of 1 to {high} characters")
    return value


def _choice(name: str, value, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise CanvasError(422, f"{name} is one of {', '.join(choices)}; {value!r} is not")
    return value


def _integer(name: str, value, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CanvasError(422, f"{name} takes a whole number from {low} to {high}")
    return value


def _metric(value) -> str:
    if value != FEEDBACK and not (isinstance(value, str) and METRIC.match(value)):
        raise CanvasError(
            422, f"test_card.metric is a signal's key, as kb.search.zero_result_rate, or {FEEDBACK}; {value!r} is not"
        )
    return value


def _threshold(value) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise CanvasError(422, "test_card.threshold takes a finite number")
    return value


def _deadline(value) -> str:
    try:
        return (value if isinstance(value, date) else date.fromisoformat(str(value))).isoformat()
    except ValueError:
        raise CanvasError(422, f"test_card.deadline takes a day as YYYY-MM-DD; {value!r} is not") from None


def _links(name: str, value, one) -> tuple[str, ...]:
    """The links ``value`` gives, each checked by ``one``, without repeats (as ``code_key`` compares them), sorted."""
    if not isinstance(value, list | tuple) or len(value) > MAX_LINKS:
        raise CanvasError(422, f"{name} takes a list of at most {MAX_LINKS}")
    found: dict[str, str] = {}
    for item in value:
        checked = one(item)
        found.setdefault(code_key(checked), checked)
    return tuple(sorted(found.values(), key=code_key))


def _use_case(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CODE_CHARS:
        raise CanvasError(
            422, f"use_cases takes the codes of use cases, as UC ORD-1, of 1 to {MAX_CODE_CHARS} characters"
        )
    return " ".join(value.split())


def _signal_key(value) -> str:
    if not (isinstance(value, str) and METRIC.match(value)):
        raise CanvasError(
            422, f"signal_keys takes the keys of signals, as kb.search.zero_result_rate; {value!r} is not"
        )
    return value


def _problem(value) -> str:
    if not (isinstance(value, str) and PROBLEM.match(value.strip())):
        raise CanvasError(422, f"problems takes the codes of problems of the project, as P-3; {value!r} is not")
    return value.strip()


def _opportunity(value) -> str:
    if not (isinstance(value, str) and OPPORTUNITY.match(value.strip())):
        raise CanvasError(
            422, f"opportunities takes the codes of opportunities of the project, as O-3; {value!r} is not"
        )
    return value.strip()


def checked_use_cases(value) -> tuple[str, ...]:
    """The use case codes ``value`` gives, each with its whitespace runs as one space, sorted, without repeats."""
    return _links("use_cases", value, _use_case)


def checked_signal_keys(value) -> tuple[str, ...]:
    """The signal keys ``value`` gives, sorted, without repeats."""
    return _links("signal_keys", value, _signal_key)


def checked_problems(value) -> tuple[str, ...]:
    """The problem codes ``value`` gives, by number, without repeats."""
    if not isinstance(value, list | tuple) or len(value) > MAX_LINKS:
        raise CanvasError(422, f"problems takes a list of at most {MAX_LINKS}")
    found = {_problem(item) for item in value}
    return tuple(sorted(found, key=problem_model.number_of))


def checked_opportunities(value) -> tuple[str, ...]:
    """The opportunity codes ``value`` gives, by number, without repeats."""
    if not isinstance(value, list | tuple) or len(value) > MAX_LINKS:
        raise CanvasError(422, f"opportunities takes a list of at most {MAX_LINKS}")
    found = {_opportunity(item) for item in value}
    return tuple(sorted(found, key=opportunity_model.number_of))


CARD_CHECKS = {  # field of a test card -> what checks it and gives the value kept
    "hypothesis": lambda v: _text("test_card.hypothesis", v, MAX_CARD_TEXT_CHARS),
    "test": lambda v: _text("test_card.test", v, MAX_CARD_TEXT_CHARS),
    "metric": _metric,
    "aggregate": lambda v: _choice("test_card.aggregate", v, AGGREGATES),
    "window_days": lambda v: _integer("test_card.window_days", v, 1, MAX_WINDOW_DAYS),
    "comparator": lambda v: _choice("test_card.comparator", v, COMPARATORS),
    "threshold": _threshold,
    "min_samples": lambda v: _integer("test_card.min_samples", v, 1, MAX_SAMPLES),
    "tenant": lambda v: _text("test_card.tenant", v, MAX_TENANT_CHARS),
    "deadline": _deadline,
}


def checked_card(value) -> dict | None:
    """The test card ``value`` gives, its fields checked and in their order; None for none, or one with no field."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CanvasError(422, "test_card takes an object")
    unknown = sorted(set(value) - set(CARD_FIELDS))
    if unknown:
        raise CanvasError(
            422, f"a test card has no field {', '.join(unknown)}; its fields are {', '.join(CARD_FIELDS)}"
        )
    card = {name: CARD_CHECKS[name](value[name]) for name in CARD_FIELDS if value.get(name) is not None}
    return card or None


CHECKS = {  # field of a claim -> what checks it and gives the value kept
    "cell": lambda v: _choice("cell", v, CELLS),
    "position": lambda v: _integer("position", v, 0, 1_000_000),
    "text": lambda v: _text("text", v, MAX_TEXT_CHARS),
    "status": lambda v: _choice("status", v, STATUSES),
    "confidence": lambda v: _choice("confidence", v, CONFIDENCES),
    "delivery": lambda v: _choice("delivery", v, DELIVERIES),
    "test_card": checked_card,
    "use_cases": checked_use_cases,
    "signal_keys": checked_signal_keys,
    "problems": checked_problems,
    "opportunities": checked_opportunities,
}


def changed(content: Content, values: dict) -> Content:
    """``content`` with the fields ``values`` sets, each checked; 422 for a field a claim does not have."""
    unknown = sorted(set(values) - set(FIELDS))
    if unknown:
        raise CanvasError(422, f"a claim has no field {', '.join(unknown)}; its fields are {', '.join(FIELDS)}")
    checked = {}
    for name, value in values.items():
        if name == "archived":
            if not isinstance(value, bool):
                raise CanvasError(422, "archived takes true or false")
            checked[name] = value
        elif name in LINKS:
            checked[name] = CHECKS[name](value or [])  # null or [] links nothing
        elif name == "test_card" or value is not None:
            checked[name] = CHECKS[name](value)
        else:
            raise CanvasError(422, f"{name} cannot be cleared")
    return replace(content, **checked)


def _flat(content: Content | None) -> dict:
    if content is None:
        return {}
    values = {name: getattr(content, name) for name in FIELDS if name != "test_card"}
    values.update({name: list(getattr(content, name)) for name in LINKS})
    card = content.test_card or {}
    values.update({f"test_card.{name}": card.get(name) for name in CARD_FIELDS})
    return values


def diff(before: Content | None, after: Content | None) -> list[dict]:
    """The fields that differ from ``before`` to ``after``, a test card's field by field (``test_card.deadline``),
    each as ``{field, before, after}``; every field of ``after`` when there is no ``before``."""
    old, new = _flat(before), _flat(after)
    names = [name for name in (*FIELDS, *(f"test_card.{n}" for n in CARD_FIELDS)) if name != "test_card"]
    return [
        {"field": name, "before": old.get(name), "after": new.get(name)}
        for name in names
        if old.get(name) != new.get(name)
    ]
