"""The usage windows of a model account that no run's CLI reports: what Z.ai's Coding Plan says of its own quota, and
the weekly budget an owner gives an account whose provider has no quota API. Standard library only: the server keeps
the windows (``evo_agents.hub.server.accounts.service.quota``), the command line and the tests read the same rules.

Both end in the shape a run's CLI reports (``accounts.last_usage``, ``rate_limits``): ``windows`` of ``five_hour`` and
``seven_day``, each the share of its limit used (0 to 1) and when it resets (UTC, ISO 8601), with ``status``
(``allowed``, or ``rejected`` once a window is used up, ``rate_limit_type`` naming it and ``resets_at`` its reset) and
``utilization``, the highest share among them. ``quota-pacing`` (``evo_agents.hub.accounts``) reads them alike.

**Z.ai.** GET ``<origin>/api/monitor/usage/quota/limit`` with the account's key answers ``data.limits``, a list of
limits, each with its ``unit`` (3 for the five-hour window, 6 for the weekly one; or a name such as ``5_HOUR`` or
``WEEKLY`` in ``unit``, ``type`` or ``window``), the ``percentage`` used (0 to 100) and ``nextResetTime``, in
milliseconds since the epoch. Other limits (the monthly calls of its tools) are left out.

**Weekly budget.** An owner may give an account ``weekly_budget_usd`` and an instant its weeks start from,
``weekly_reset_at`` (none: Monday 00:00 UTC); a week starts every 7 days before and after it. The cost of the runs
that took the account since the week began, over the budget, is its ``seven_day`` window, which resets when the week
ends, and replaces any the account reported: the owner said what a week of it holds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_agents.hub.accounts import FIVE_HOUR, SEVEN_DAY

ZAI_PROVIDER = "zai"
ZAI_QUOTA_PATH = "/api/monitor/usage/quota/limit"
ZAI_UNITS = {3: FIVE_HOUR, 6: SEVEN_DAY}
ZAI_NAMES = {"5_HOUR": FIVE_HOUR, "FIVE_HOUR": FIVE_HOUR, "WEEKLY": SEVEN_DAY, "WEEK": SEVEN_DAY, "7_DAY": SEVEN_DAY}
WEEK = timedelta(days=7)
MONDAY = datetime(2024, 1, 1, tzinfo=UTC)  # a Monday 00:00 UTC: the weeks of a budget with no reset time given
MAX_PERCENT = 100.0


def _number(value) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _zai_window(limit: dict) -> str | None:
    """The window a limit of Z.ai is, by its unit's number or a name; None for another."""
    unit = limit.get("unit")
    if isinstance(unit, int) and not isinstance(unit, bool):
        return ZAI_UNITS.get(unit)
    for key in ("unit", "type", "window"):
        value = limit.get(key)
        if isinstance(value, str) and value.upper() in ZAI_NAMES:
            return ZAI_NAMES[value.upper()]
    return None


def _zai_reset(limit: dict) -> str | None:
    ms = _number(limit.get("nextResetTime"))
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _zai_share(limit: dict) -> float | None:
    percent = _number(limit.get("percentage"))
    if percent is None:
        used, total = _number(limit.get("currentValue")), _number(limit.get("usage"))
        percent = used / total * MAX_PERCENT if used is not None and total else None
    return None if percent is None else min(max(percent / MAX_PERCENT, 0.0), 1.0)


def zai_windows(body) -> dict[str, dict]:
    """The windows an answer of Z.ai's quota API holds, by name (``five_hour``, ``seven_day``)."""
    data = body.get("data") if isinstance(body, dict) else None
    limits = data.get("limits") if isinstance(data, dict) else None
    windows: dict[str, dict] = {}
    for limit in limits if isinstance(limits, list) else []:
        name = _zai_window(limit) if isinstance(limit, dict) else None
        share = _zai_share(limit) if name else None
        if name is None or share is None:
            continue
        window: dict = {"utilization": share}
        reset = _zai_reset(limit)
        if reset is not None:
            window["resets_at"] = reset
        windows[name] = window
    return windows


def usage_of(windows: dict[str, dict]) -> dict:
    """The usage ``windows`` make, as the module says: rejected when one is used up, the one that resets last."""
    usage: dict = {"status": "allowed", "windows": windows}
    shares = [w["utilization"] for w in windows.values()]
    if shares:
        usage["utilization"] = max(shares)
    full = [(w.get("resets_at") or "", name) for name, w in windows.items() if w["utilization"] >= 1.0]
    if full:
        reset, name = max(full)
        usage |= {"status": "rejected", "rate_limit_type": name}
        if reset:
            usage["resets_at"] = reset
    return usage


def zai_usage(body) -> dict | None:
    """The usage an answer of Z.ai's quota API says, or None when it lists no window."""
    windows = zai_windows(body)
    return usage_of(windows) if windows else None


def week_bounds(anchor: datetime | None, now: datetime) -> tuple[datetime, datetime]:
    """(start, end) of the week of a budget ``now`` is in, its weeks starting every 7 days from ``anchor``."""
    anchor = anchor or MONDAY
    weeks = (now - anchor) // WEEK
    start = anchor + weeks * WEEK
    return start, start + WEEK


def budget_window(cost_usd: float, budget_usd: float, end: datetime) -> dict:
    """The ``seven_day`` window of a weekly budget: the cost over the budget, resetting when the week ends."""
    share = min(max(cost_usd / budget_usd, 0.0), 1.0) if budget_usd > 0 else 1.0
    return {"utilization": round(share, 6), "resets_at": end.astimezone(UTC).isoformat()}


def with_budget(usage, window: dict) -> dict:
    """``usage`` (an account's last, or None) with ``window`` as its ``seven_day``, as the module says."""
    kept = dict(usage) if isinstance(usage, dict) else {"status": "allowed"}
    given = kept.get("windows")
    windows: dict[str, dict] = dict(given) if isinstance(given, dict) else {}
    windows[SEVEN_DAY] = window
    merged = usage_of(windows)
    if kept.get("status") == "rejected" and merged["status"] != "rejected":
        return kept | {"windows": windows}  # a limit the CLI reported stays
    return kept | merged
