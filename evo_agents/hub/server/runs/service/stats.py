"""Run stats by day, for the web's charts: GET /v1/projects/{p}/runs/stats counts the runs of the plans the caller may
read that ended on each of the last days in UTC, in each end state, with the percentiles of how long they ran and the
tokens of their usage, read as the run page's usage card reads it (``_usage_parts``)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Date, Numeric, case, cast, column, extract, func, or_, select, true
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB

from evo_agents.hub import runs, tables
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import RunDay, RunFigures, RunStats
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import Principal

# The keys of a run's usage the usage card reads (web/src/components/runs/usage-model.ts), each runtime's own
USAGE_KEYS = (
    *("input_tokens", "cache_read_input_tokens", "output_tokens", "output_tokens_details"),  # Claude Code
    *("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens"),  # Codex, as it streams them
    *("cached_input_tokens", "reasoning_output_tokens"),  # Codex, as its worker reports the run's total
    *("input", "output", "reasoning", "cache_read", "cache"),  # opencode
)


def _usage_number(value):
    """``value`` (a jsonb expression) when it is a positive number, else 0, as the usage card reads a number."""
    return case((func.jsonb_typeof(value) == "number", func.greatest(cast(value, Numeric), 0)), else_=0)


def _usage_either(first, second):
    """The number of ``first`` when it holds a value, null or missing being none, else the number of ``second``: the
    card's ``first ?? second``."""
    return case((first.is_not(None), _usage_number(first)), else_=_usage_number(second))


def _usage_keys(usage):
    """The keys of USAGE_KEYS taken apart once from a run's ``usage`` (``k``), each as jsonb: null for a key it does
    not have, and for usage that is not a JSON object."""
    whole = case((func.jsonb_typeof(usage) == "object", usage))
    keys = func.jsonb_to_record(whole).table_valued(*(column(name, JSONB) for name in USAGE_KEYS))
    return keys.render_derived(name="k", with_types=True).lateral("k")


def _usage_shape(k):
    """The shape that reported the usage of ``k``: codex, claude or opencode, null for none the card knows."""

    def has(*names: str):
        return or_(*(func.jsonb_typeof(k.c[name]) == "number" for name in names))

    codex = has("inputTokens", "outputTokens", "cachedInputTokens", "cached_input_tokens", "reasoning_output_tokens")
    claude = has("input_tokens", "output_tokens", "cache_read_input_tokens")
    opencode = or_(has("input", "output", "reasoning", "cache_read"), func.jsonb_typeof(k.c.cache) == "object")
    return case((codex, "codex"), (claude, "claude"), (opencode, "opencode"))


def _usage_parts(k, shape) -> dict:
    """A run's usage read as the usage card's readTokens reads it, from the keys of ``k`` by its ``shape``: the whole
    input, the part of it read from the cache, the whole output and the reasoning part of it. Codex counts the cache
    in its input and reasoning in its output, Claude Code counts thinking in its output, and opencode keeps the four
    apart."""

    def by_shape(claude, codex, opencode):
        return case({"claude": claude, "codex": codex, "opencode": opencode}, value=shape, else_=0)

    number, either = _usage_number, _usage_either
    return {
        "whole_input": by_shape(number(k.c.input_tokens), either(k.c.inputTokens, k.c.input_tokens), number(k.c.input)),
        "cached": by_shape(
            number(k.c.cache_read_input_tokens),
            either(k.c.cachedInputTokens, k.c.cached_input_tokens),
            either(k.c.cache_read, k.c.cache["read"]),
        ),
        "whole_output": by_shape(
            number(k.c.output_tokens), either(k.c.outputTokens, k.c.output_tokens), number(k.c.output)
        ),
        "thought": by_shape(
            number(k.c.output_tokens_details["thinking_tokens"]),
            either(k.c.reasoningOutputTokens, k.c.reasoning_output_tokens),
            number(k.c.reasoning),
        ),
    }


def _run_stats(project_id: int, plans: list[str], since: datetime, until: datetime):
    """The runs of ``plans`` that ended in [since, until), by UTC day and over the whole span (the row whose
    whole_span is true): how many in each end state, the percentiles of how long they ran and their tokens. Each run's
    usage is taken apart once (``_usage_keys``), its shape read once (``s``) and its four numbers once (``u``)."""
    r = tables.runs
    k = _usage_keys(r.c.usage)
    s = select(_usage_shape(k).label("shape")).correlate(k).lateral("s")
    u = select(*(part.label(name) for name, part in _usage_parts(k, s.c.shape).items())).correlate(k, s).lateral("u")
    began = func.coalesce(r.c.started_at, r.c.leased_at)
    ran = func.greatest(extract("epoch", r.c.finished_at - began), 0)
    codex, opencode = s.c.shape == "codex", s.c.shape == "opencode"
    cache_read = func.least(u.c.cached, u.c.whole_input)
    reasoning = func.least(u.c.thought, u.c.whole_output)
    ended = (
        select(
            cast(func.timezone("UTC", r.c.finished_at), Date).label("day"),
            r.c.state,
            case((began.is_not(None), cast(ran, DOUBLE_PRECISION))).label("seconds"),
            s.c.shape,
            case((codex, cache_read), else_=u.c.cached).label("cache_read"),
            case((codex, u.c.whole_input - cache_read), else_=u.c.whole_input).label("input"),
            case((opencode, u.c.thought), else_=reasoning).label("reasoning"),
            case((opencode, u.c.whole_output), else_=u.c.whole_output - reasoning).label("output"),
        )
        .select_from(r)
        .join(k, true())
        .join(s, true())
        .join(u, true())
        .where(
            r.c.project_id == project_id,
            r.c.plan_id.in_(plans),
            r.c.state.in_(runs.TERMINAL_STATES),
            r.c.finished_at >= since,
            r.c.finished_at < until,
        )
        .cte("ended")
    )
    e = ended.c
    return select(
        e.day,
        (func.grouping(e.day) == 1).label("whole_span"),
        *(func.count().filter(e.state == state).label(state) for state in runs.TERMINAL_STATES),
        func.percentile_cont(0.5).within_group(e.seconds).label("p50_seconds"),
        func.percentile_cont(0.9).within_group(e.seconds).label("p90_seconds"),
        func.sum(e.input).label("input_tokens"),
        func.sum(e.output).label("output_tokens"),
        func.sum(e.cache_read).label("cache_read_tokens"),
        func.sum(e.reasoning).label("reasoning_tokens"),
        func.count(e.shape).label("runs_with_usage"),
    ).group_by(func.rollup(e.day))


def _seconds(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _figures(row) -> RunFigures:
    """A row of ``_run_stats``, by column name: a sum over no run (null) is 0, a percentile kept to the
    millisecond."""
    return RunFigures(
        **{state: row[state] for state in runs.TERMINAL_STATES},
        p50_seconds=_seconds(row["p50_seconds"]),
        p90_seconds=_seconds(row["p90_seconds"]),
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cache_read_tokens=int(row["cache_read_tokens"] or 0),
        reasoning_tokens=int(row["reasoning_tokens"] or 0),
        runs_with_usage=row["runs_with_usage"],
    )


NO_RUN = _figures(
    dict.fromkeys(runs.TERMINAL_STATES, 0)
    | dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "runs_with_usage"), 0)
    | {"p50_seconds": None, "p90_seconds": None}
)


async def daily_stats(engine, user: Principal, project: str, days: int, sink: str | None) -> RunStats:
    """The runs of the plans the caller may read that ended on each of the last ``days`` days in UTC: how many in
    each end state, how long they ran and the tokens they used."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        today: date = (await conn.execute(select(cast(func.timezone("UTC", func.now()), Date)))).scalar_one()
        first = today - timedelta(days=days - 1)
        since = datetime.combine(first, time.min, tzinfo=UTC)
        until = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
        rows = (await conn.execute(_run_stats(access.project_id, plans, since, until))).all()
    by_day, total = {}, NO_RUN
    for row in rows:
        if row.whole_span:
            total = _figures(row._mapping)
        else:
            by_day[row.day] = _figures(row._mapping)
    shown = [first + timedelta(days=back) for back in range(days)]
    return RunStats(
        project=access.name,
        days=days,
        first_day=first,
        last_day=today,
        by_day=[RunDay(day=day, **by_day.get(day, NO_RUN).model_dump()) for day in shown],
        total=total,
    )
