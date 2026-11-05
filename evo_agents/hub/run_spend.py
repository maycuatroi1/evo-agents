"""What a run spent: its tokens and its cost by model, its own share of the agent sessions it went on in.

A runtime reports a run's usage as it goes, in ``usage_update`` events (``evo_agents.worker.runtimes``), and the hub
keeps what each run spent from them as they come (``evo_agents.hub.spend_store``), so a run shows its tokens and
cost while it runs, waits or is parked, and after it ended in any state, not only once its worker reported its end.

Reports. Each event's body reads as one report (``reports_of``), of one agent session (its ``session_id``, else the
run's) and, for each model it names, figures of two scopes: a ``delta`` is what the report alone spent; a ``total`` is
what the session spent so far, which goes on across the runs that go on in the session (Claude Code restores it from
the session's transcript when it resumes it) and starts from 0 in a process that does not restore it. The runtimes
report so:

- Claude Code: ``models`` (its ``modelUsage``), each model's tokens and cost so far in the session (total), with the
  session's cost so far (``cost``, total) and the turn's tokens (``usage``, delta). A worker of before ``models`` sent
  the turn's tokens (delta) and the session's cost (total), of the run's model.
- Codex: the thread's tokens so far (total) with the last turn's beside them (``last``); no cost.
- opencode: each step's tokens and cost (delta), of the step's ``model`` when it names one.
- an ACP agent: the session's cost so far (total).
- a local session (``evo_agents.hub.local_sync``): each API call's tokens per model (``models``, delta, no cost), and
  each ``cost-state`` of Claude Code's transcript (``models`` and ``cost``, total).

Own share. A total counts what earlier runs of the session spent too, so the ledger takes the run's share of it:
from where the session stood when the run began (``base``, the totals the latest earlier run of the session reported
last, its ``ends``), each total report adds its step from the one before. A report either goes on from the one before
or opens a process that started from 0 (``goes_on``): it goes on when its step is nearer to what its own turn spent
(``expected``: the turn's tokens at the model's price, or Codex's last turn's tokens) than its total is, or, when that
is not known, when its total did not fall. Deltas add up; a total takes in the deltas before it, which its count holds.

Cost. A cost the runtime reported is ``reported``. Tokens no reported cost covers (a runtime that reports none, the
messages of a local session since its last ``cost-state``) are priced at the model's list price (PRICES) as
``estimated``; a model without a price there has none. Tokens: ``input`` is what was not read from the cache,
``cache_read`` and ``cache_write`` the cache's (``cache_write_1h`` the part written for an hour), ``output`` holds the
reasoning (``reasoning``, the part of it the runtime counts apart).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

TOKENS = ("input", "output", "cache_read", "cache_write")  # what a run is billed for, apart from each other
PARTS = ("cache_write_1h", "reasoning")  # parts of cache_write and output
FIGURES = (*TOKENS, *PARTS)
UNKNOWN = ""  # the model or session of a report that names none
SYSTEM_KINDS = ("output", "system")  # the kinds of the events whose raw init message names Claude Code's model


@dataclass(frozen=True)
class Price:
    """USD per million tokens. A cache write costs 1.25 times the input for five minutes, twice for an hour."""

    input: float
    output: float
    cache_read: float

    def of(self, tokens: Mapping[str, float]) -> float:
        written = tokens.get("cache_write", 0)
        hour = min(tokens.get("cache_write_1h", 0), written)
        cost = tokens.get("input", 0) * self.input + tokens.get("output", 0) * self.output
        cost += tokens.get("cache_read", 0) * self.cache_read + (written - hour) * self.input * 1.25
        return (cost + hour * self.input * 2) / 1_000_000


# Anthropic's list prices of the API (the claude-api skill's table of 2026-10-06; cache reads 0.1 times the input, but
# 0.025 for Fable 5.1 and Mythos 5.1 and 0.05 for Opus 5.5). Fast mode, long context and partner platforms are left out.
PRICES = {
    "claude-fable-5-1": Price(10, 50, 0.25),
    "claude-mythos-5-1": Price(10, 50, 0.25),
    "claude-fable-5": Price(10, 50, 1.0),
    "claude-opus-5-5": Price(4, 20, 0.2),
    "claude-opus-5": Price(5, 25, 0.5),
    "claude-opus-4-8": Price(5, 25, 0.5),
    "claude-opus-4-7": Price(5, 25, 0.5),
    "claude-opus-4-6": Price(5, 25, 0.5),
    "claude-sonnet-5-5": Price(2, 10, 0.2),
    "claude-sonnet-5": Price(2, 10, 0.2),
    "claude-sonnet-4-6": Price(3, 15, 0.3),
    "claude-haiku-5-5": Price(0.1, 0.5, 0.01),
    "claude-haiku-4-5": Price(1, 5, 0.1),
}
_PROVIDER = re.compile(r"^(?:[a-z]{2}\.)?(?:anthropic[./])")  # anthropic/..., anthropic...., us.anthropic....
_SUFFIX = re.compile(r"(?:\[[^\]]*\]|[-@]\d{8})$")  # [1m], a date


def model_key(model) -> str:
    """``model`` as the ledger keys it: stripped and lower case; UNKNOWN for no name."""
    return model.strip().lower() if isinstance(model, str) and model.strip() else UNKNOWN


def price_of(model: str) -> Price | None:
    """The list price of ``model``, its provider prefix, context suffix and date left out; None for a model not in
    PRICES."""
    name = _PROVIDER.sub("", model_key(model))
    while (bare := _SUFFIX.sub("", name)) != name:
        name = bare
    return PRICES.get(name)


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0.0


def _map(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _usd(cost) -> float | None:
    """The amount of a ``{"amount", "currency": "USD"}`` or of a bare number; None for any other."""
    if isinstance(cost, Mapping):
        if cost.get("currency") not in (None, "USD"):
            return None
        cost = cost.get("amount")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return float(cost)
    return None


def _clean(tokens: Mapping[str, float]) -> dict[str, float]:
    return {key: value for key, value in tokens.items() if value}


def claude_tokens(usage: Mapping) -> dict[str, float]:
    """The tokens of a Claude usage (the API's, snake_case): the cache's apart, thinking a part of output."""
    written = _map(usage.get("cache_creation"))
    return _clean(
        {
            "input": _number(usage.get("input_tokens")),
            "output": _number(usage.get("output_tokens")),
            "cache_read": _number(usage.get("cache_read_input_tokens")),
            "cache_write": _number(usage.get("cache_creation_input_tokens")),
            "cache_write_1h": _number(written.get("ephemeral_1h_input_tokens")),
            "reasoning": _number(_map(usage.get("output_tokens_details")).get("thinking_tokens")),
        }
    )


def model_usage_tokens(usage: Mapping) -> dict[str, float]:
    """The tokens of one model's ``modelUsage`` (Claude Code's, camelCase)."""
    return _clean(
        {
            "input": _number(usage.get("inputTokens")),
            "output": _number(usage.get("outputTokens")),
            "cache_read": _number(usage.get("cacheReadInputTokens")),
            "cache_write": _number(usage.get("cacheCreationInputTokens")),
            "cache_write_1h": _number(usage.get("cacheCreation1hInputTokens")),
            "reasoning": _number(usage.get("thinkingTokens")),
        }
    )


def codex_tokens(usage: Mapping) -> dict[str, float]:
    """The tokens of Codex's usage, camelCase or snake_case: its input holds the cached input, its output reasoning."""

    def pick(camel: str, snake: str) -> float:
        return _number(usage.get(camel, usage.get(snake)))

    given = pick("inputTokens", "input_tokens")
    cached = min(pick("cachedInputTokens", "cached_input_tokens"), given)
    output = pick("outputTokens", "output_tokens")
    reasoning = min(pick("reasoningOutputTokens", "reasoning_output_tokens"), output)
    return _clean({"input": given - cached, "cache_read": cached, "output": output, "reasoning": reasoning})


def opencode_tokens(usage: Mapping) -> dict[str, float]:
    """The tokens of an opencode step: reasoning apart from output there, a part of it here."""
    cache = _map(usage.get("cache"))
    reasoning = _number(usage.get("reasoning"))
    return _clean(
        {
            "input": _number(usage.get("input")),
            "output": _number(usage.get("output")) + reasoning,
            "cache_read": _number(usage.get("cache_read", cache.get("read"))),
            "cache_write": _number(usage.get("cache_write", cache.get("write"))),
            "reasoning": reasoning,
        }
    )


@dataclass
class Part:
    """What one report says of one model: its tokens and its cost, each a delta or a total, or absent."""

    model: str
    tokens: dict[str, float] = field(default_factory=dict)
    tokens_total: bool = False
    cost: float | None = None
    cost_total: bool = False


@dataclass
class Report:
    """One usage report of a session: its parts, and what tells whether it goes on from the one before (``gauge``,
    the session's running total it reports, with ``expected``, what its own turn spent, in the gauge's unit)."""

    session: str
    parts: list[Part]
    gauge: float | None = None
    expected: float | None = None


def _total(tokens: Mapping[str, float]) -> float:
    return sum(tokens.get(key, 0) for key in TOKENS)


def _claude_report(body: Mapping, session: str, model: str) -> Report | None:
    usage, cost, models = _map(body.get("usage")), _usd(body.get("cost")), _map(body.get("models"))
    turn = claude_tokens(usage)
    if models:  # each model's totals of the session; the turn's tokens only tell how much the turn spent
        parts = [
            Part(model_key(name), model_usage_tokens(_map(figures)), True, _usd(_map(figures).get("costUSD")), True)
            for name, figures in models.items()
        ]
        main = max(parts, key=lambda part: part.cost or 0.0).model
    else:
        parts = [Part(model, turn, False, cost, True)] if turn or cost is not None else []
        main = model
    if not parts:
        return None
    price = price_of(main)
    if cost is None and models:  # the models' costs add up to the session's
        reported = [part.cost for part in parts if part.cost is not None]
        cost = sum(reported) if reported else None
    expected = price.of(turn) if price is not None and turn else None
    return Report(session, parts, cost, expected)


def _codex_report(body: Mapping, session: str, model: str) -> Report | None:
    tokens = codex_tokens(_map(body.get("usage")))
    if not tokens:
        return None
    last = _map(body.get("last"))
    return Report(session, [Part(model, tokens, True)], _total(tokens), _total(codex_tokens(last)) if last else None)


def _opencode_report(body: Mapping, session: str, model: str) -> Report | None:
    tokens, cost = opencode_tokens(_map(body.get("usage"))), _usd(body.get("cost"))
    if not tokens and cost is None:
        return None
    return Report(session, [Part(model_key(body.get("model")) or model, tokens, False, cost, False)])


def _acp_report(body: Mapping, session: str, model: str) -> Report | None:
    tokens, cost = claude_tokens(_map(body.get("usage"))), _usd(body.get("cost"))
    if not tokens and cost is None:
        return None
    return Report(session, [Part(model, tokens, False, cost, True)], cost)


def _local_report(body: Mapping, session: str) -> Report | None:
    models, cost = _map(body.get("models")), _usd(body.get("cost"))
    total = body.get("scope") == "total"
    parts = []
    for name, figures in models.items():
        part_cost = _usd(_map(figures).get("costUSD"))
        parts.append(Part(model_key(name), model_usage_tokens(_map(figures)), total, part_cost, total))
    if not parts:
        return None
    if total and cost is None:
        reported = [part.cost for part in parts if part.cost is not None]
        cost = sum(reported) if reported else None
    return Report(session, parts, cost if total else None)


def report_of(runtime: str | None, body, session: str, model: str) -> Report | None:
    """The report of one ``usage_update`` event's ``body`` of a run of ``runtime``, of the run's ``session`` and
    ``model`` unless the body names its own; None for a body that reports nothing."""
    if not isinstance(body, Mapping):
        return None
    session = body["session_id"] if isinstance(body.get("session_id"), str) and body["session_id"] else session
    if body.get("source") == "local":
        return _local_report(body, session)
    model = model_key(body.get("model")) or model
    if runtime == "claude-code":
        return _claude_report(body, session, model)
    if runtime == "codex":
        return _codex_report(body, session, model)
    if runtime == "opencode":
        return _opencode_report(body, session, model)
    return _acp_report(body, session, model)


def usage_report(runtime: str | None, usage, session: str, model: str) -> Report | None:
    """The report of a run's ``usage`` as its worker reported it with the run's end, for a run whose usage reports
    are not kept (their events pruned, or none sent): Claude Code's tokens of the run with the session's cost so far,
    Codex's thread total, opencode's sum of steps, an ACP agent's session cost."""
    if not isinstance(usage, Mapping):
        return None
    if runtime == "claude-code":
        body = {"usage": usage, "cost": usage.get("total_cost_usd")}
    elif runtime == "opencode":
        body = {"usage": usage, "cost": usage.get("cost")}
    elif runtime == "codex":
        body = {"usage": usage}
    else:
        body = {"usage": {}, "cost": usage.get("cost")}
    return report_of(runtime, body, session, model)


def init_model(body) -> str | None:
    """The model a ``system`` init message of Claude Code names, from an event's body (``raw``); None for another."""
    raw = _map(_map(body).get("raw"))
    if raw.get("type") == "system" and raw.get("subtype") == "init":
        return model_key(raw.get("model")) or None
    return None


def reports_of(runtime: str | None, events: Iterable[tuple[str, object]], session: str, model: str) -> list[Report]:
    """The reports of a run's events (kind and body, in seq order): its ``usage_update`` events, each of the model
    the latest init message before it named when it names none, else ``model``."""
    found = []
    for kind, body in events:
        if kind == "usage_update":
            report = report_of(runtime, body, session, model)
            if report is not None:
                found.append(report)
        elif kind in SYSTEM_KINDS:
            model = init_model(body) or model
    return found


def goes_on(before: float | None, now: float | None, expected: float | None) -> bool:
    """Whether a total ``now`` goes on from ``before`` rather than starting from 0 again: when its step is nearer to
    ``expected`` than ``now`` is, or, with no ``expected``, when it did not fall. True with nothing before (both read
    the same then) and with no total now (nothing to tell)."""
    if before is None or before <= 0 or now is None:
        return True
    if now < before:
        return False
    if expected is None:
        return True
    return abs(now - before - expected) <= abs(now - expected)


@dataclass
class _Series:
    """One figure of one model in a session: the total it stood at, what the run's totals added, and the deltas since
    the latest total."""

    last: float | None = None
    gained: float = 0.0
    after: float = 0.0

    def total(self, value: float, on: bool) -> None:
        before = self.last or 0.0
        self.gained += value - before if on and value >= before else value
        self.last, self.after = value, 0.0

    def delta(self, value: float) -> None:
        self.after += value

    def own(self) -> float:
        return self.gained + self.after


@dataclass
class _Model:
    """What a run spent on one model in one session, as its reports come."""

    tokens: dict[str, _Series] = field(default_factory=dict)
    cost: _Series = field(default_factory=_Series)
    priced: bool = False  # whether any report gave a cost
    uncovered: dict[str, float] = field(default_factory=dict)  # tokens since the latest cost, with none of their own
    reports: int = 0

    def start(self, ends: Mapping) -> None:
        self.cost.last = _number(ends.get("cost")) if ends.get("cost") is not None else None
        for key, value in _map(ends.get("tokens")).items():
            self.tokens[key] = _Series(last=_number(value))

    def take(self, part: Part, on: bool) -> None:
        self.reports += 1
        for key in FIGURES:
            series = self.tokens.setdefault(key, _Series())
            value = part.tokens.get(key, 0.0)
            if part.tokens_total:
                series.total(value, on)
            elif value:
                series.delta(value)
        if part.cost is None:
            for key, value in part.tokens.items():
                if not part.tokens_total:
                    self.uncovered[key] = self.uncovered.get(key, 0.0) + value
            return
        self.priced, self.uncovered = True, {}
        if part.cost_total:
            self.cost.total(part.cost, on)
        else:
            self.cost.delta(part.cost)

    def restart(self) -> None:
        """A new process of the session: every total of this model starts from 0."""
        for series in (*self.tokens.values(), self.cost):
            series.last = None if series.last is None else 0.0

    def ends(self) -> dict:
        tokens = {key: series.last for key, series in self.tokens.items() if series.last is not None}
        return {"cost": self.cost.last, "tokens": tokens}


@dataclass(frozen=True)
class Spend:
    """What a run spent on one model in one session."""

    session: str
    model: str
    tokens: dict[str, int]
    reported_usd: float | None
    estimated_usd: float | None
    reports: int
    ends: dict  # the totals it reported last, and the session's gauge: where the session stands for the next run

    @property
    def cost_usd(self) -> float | None:
        if self.reported_usd is None and self.estimated_usd is None:
            return None
        return (self.reported_usd or 0.0) + (self.estimated_usd or 0.0)


def _estimate(model: str, tokens: Mapping[str, float]) -> float | None:
    price = price_of(model)
    return price.of(tokens) if price is not None and _total(tokens) else None


def ledger(reports: Iterable[Report], bases: Mapping[str, Mapping] | None = None) -> list[Spend]:
    """What a run spent, by session and model, from its ``reports`` in order and the ``bases`` of its sessions (each
    session's gauge and its models' totals where an earlier run of it left them, as its Spend ends say)."""
    bases = bases or {}
    sessions: dict[str, dict[str, _Model]] = {}
    gauges: dict[str, float | None] = {}
    for report in reports:
        models = sessions.get(report.session)
        if models is None:
            base = _map(bases.get(report.session)) if report.session else {}
            models = sessions[report.session] = {}
            for name, ends in _map(base.get("models")).items():
                models.setdefault(name, _Model()).start(_map(ends))
            gauges[report.session] = _number(base.get("gauge")) if base.get("gauge") is not None else None
        on = goes_on(gauges.get(report.session), report.gauge, report.expected)
        if not on:
            for found in models.values():
                found.restart()
        if report.gauge is not None:
            gauges[report.session] = report.gauge
        for part in report.parts:
            models.setdefault(part.model, _Model()).take(part, on)
    spent = []
    for session, models in sessions.items():
        for name, found in models.items():
            if not found.reports:
                continue
            tokens = {key: round(series.own()) for key, series in found.tokens.items() if series.own() > 0}
            reported = found.cost.own() if found.priced else None
            estimated = _estimate(name, found.uncovered if found.priced else tokens)
            ends = {**found.ends(), "gauge": gauges.get(session)}
            spent.append(Spend(session, name, tokens, reported, estimated, found.reports, ends))
    return spent


def summary(spent: Iterable[Spend]) -> dict:
    """A run's spend in one: its tokens and its cost (reported and estimated) added up, and each model's, the dearest
    first."""
    by_model: dict[str, dict] = {}
    for item in spent:
        entry = by_model.setdefault(
            item.model, {"model": item.model, "tokens": {}, "reported_usd": None, "estimated_usd": None}
        )
        for key, value in item.tokens.items():
            entry["tokens"][key] = entry["tokens"].get(key, 0) + value
        for key in ("reported_usd", "estimated_usd"):
            value = getattr(item, key)
            if value is not None:
                entry[key] = (entry[key] or 0.0) + value
    models = sorted(
        by_model.values(), key=lambda entry: -((entry["reported_usd"] or 0) + (entry["estimated_usd"] or 0))
    )
    tokens: dict[str, int] = {}
    for entry in models:
        for key, value in entry["tokens"].items():
            tokens[key] = tokens.get(key, 0) + value
    reported = [entry["reported_usd"] for entry in models if entry["reported_usd"] is not None]
    estimated = [entry["estimated_usd"] for entry in models if entry["estimated_usd"] is not None]
    return {
        "tokens": tokens,
        "reported_usd": sum(reported) if reported else None,
        "estimated_usd": sum(estimated) if estimated else None,
        "models": models,
    }
