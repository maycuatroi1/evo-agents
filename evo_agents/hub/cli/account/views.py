"""How ``evo-agents hub account`` shows an account (``docs/credentials.md``, section "Accounts"): a row of ``account
list`` (:func:`account_row`), the lines of ``account show`` (:func:`account_lines`), which ``login`` and
``oauth-login`` print too, and the reminder to renew an account that ends within RENEW_WITHIN_DAYS days, or has ended
(:func:`expiry`). Standard library only, like the rest of the client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_agents.hub import setup_token
from evo_agents.hub.accounts import RENEW_WITHIN_DAYS
from evo_agents.hub.cli.base import when
from evo_agents.hub.cli.provider_account import provider_text
from evo_agents.hub.cli.secret.bindings import where_cell
from evo_agents.hub.curator import money

# The keys of an account in `hub account list --json` and `set|pause|resume --json` (`evo-agents hub contract print`).
ACCOUNT_KEYS = (
    "name",
    "env_var",
    "provider",
    "category",
    "auth",
    "projects",
    "workers",
    "pools",
    "weight",
    "priority",
    "paused_at",
    "cooldown_until",
    "cooldown_reason",
    "last_leased_at",
    "expires_at",
    "state",
    "last_usage",
    "last_usage_at",
    "weekly_budget_usd",
    "weekly_reset_at",
    "runs_24h",
    "runs_7d",
    "cost_24h_usd",
    "cost_7d_usd",
    "held_by",
    "shares",
    "cost_by_principal",
    "gateway",
)
ACCOUNT_COLUMNS = (
    "NAME",
    "VARIABLE",
    "PROVIDER",
    "AUTH",
    "STATE",
    "WEIGHT",
    "PRIORITY",
    "COOLS UNTIL",
    "USAGE",
    "RUNS 24H/7D",
    "COST 7D",
    "HELD BY",
    "PROJECTS",
    "WORKERS",
)


def account_line(account: dict) -> str:
    """What ``account set``, ``pause`` and ``resume`` print of the account they changed."""
    return (
        f"Account {account['name']} ({account['env_var']}): {account['state']}, weight {account['weight']}, "
        f"priority {account['priority']}."
    )


def account_row(a: dict) -> tuple:
    """The row of an account under ACCOUNT_COLUMNS."""
    return (
        a["name"],
        a["env_var"],
        provider_text(a),
        "key" if a.get("gateway") else a["auth"],
        a["state"],
        a["weight"],
        a["priority"],
        when(a["cooldown_until"]),
        usage_text(a["last_usage"]),
        f"{a['runs_24h']}/{a['runs_7d']}",
        "-" if a["cost_7d_usd"] is None else money(a["cost_7d_usd"]),
        " ".join(f"#{run['run_id']}" for run in a["held_by"]) or "-",
        ", ".join(a["projects"]),
        where_cell(a),
    )


def reminders(accounts: list[dict], now: datetime) -> list[str]:
    """A line for each account due for renewal, as ``account list`` prints them under its table."""
    lines = []
    for a in accounts:
        text, due = expiry(a, now)
        if due:
            lines.append(f"Account {a['name']}: expires {text}.")
    return lines


def renew_command(account: dict) -> str:
    if account.get("gateway"):
        return f"`evo-agents hub provider-account set {account['name']} --expires ...`"
    if account["env_var"] == setup_token.ENV_VAR:
        command = "oauth-login" if account.get("auth") == "oauth" else "login"
        return f"`evo-agents hub account {command} {account['name']}`"
    return f"`evo-agents hub secret set {account['name']} --kind env --env-var {account['env_var']} ...`"


def expiry(account: dict, now: datetime) -> tuple[str, bool]:
    """When the account ends, as ``account show`` says it, and whether it is due for renewal: it ends within
    RENEW_WITHIN_DAYS days, or has ended."""
    if not account["expires_at"]:
        return "never", False
    until = f"{when(account['expires_at'])} UTC"
    if account.get("auth") == "oauth":  # the end of its access token, which its refresh token replaces
        return f"access token until {until}", False
    left = datetime.fromisoformat(account["expires_at"]) - now
    if left > timedelta(days=RENEW_WITHIN_DAYS):
        return until, False
    renew = f"renew it with {renew_command(account)}"
    if left <= timedelta(0):
        return f"{until}, already past, so no run takes it: {renew}", True
    days = f"in {left.days} day(s)" if left.days else "in less than a day"
    return f"{until}, {days}: {renew}", True


def percent(share) -> str:
    """A utilization the CLI gave, a fraction of the limit (0.85), as `` 85%``; above 1 is the limit, `` 100%``, as
    the web reads it (docs/credentials.md, section "Accounts"); empty for none."""
    if isinstance(share, bool) or not isinstance(share, int | float):
        return ""
    return f" {min(max(share, 0), 1) * 100:.0f}%"


def usage_text(usage: dict | None) -> str:
    """The last usage a run's CLI reported, short: ``five_hour 85% (allowed_warning)``; ``-`` for none."""
    if not usage:
        return "-"
    return f"{usage.get('rate_limit_type') or 'usage'}{percent(usage.get('utilization'))} ({usage['status']})"


def windows_text(usage: dict | None) -> str:
    """The windows of a last usage: ``five_hour 20%, seven_day 45% (resets 2026-11-23 00:00)``; empty for none."""
    windows = usage.get("windows") if isinstance(usage, dict) else None
    if not isinstance(windows, dict):
        return ""
    parts = []
    for name, window in sorted(windows.items()):
        if not isinstance(window, dict):
            continue
        resets = f" (resets {when(window['resets_at'])})" if window.get("resets_at") else ""
        parts.append(f"{name}{percent(window.get('utilization'))}{resets}")
    return ", ".join(parts)


def state_text(account: dict) -> str:
    state = account["state"]
    if state == "cooling":
        return f"cooling until {when(account['cooldown_until'])} UTC ({account['cooldown_reason'] or 'usage limit'})"
    if state == "paused":
        return f"paused since {when(account['paused_at'])} UTC"
    if state == "expired":
        return f"expired at {when(account['expires_at'])} UTC"
    if state == "needs-relogin":
        return f"needs a new sign-in: run `evo-agents hub account oauth-login {account['name']}`"
    return "ready"


def gateway_lines(a: dict) -> list[str]:
    """The line ``account show`` prints of a provider account (``cli.provider_account.print_gateway``); none for
    another account."""
    if not (g := a.get("gateway")):
        return []
    said = f" ({g['reason']})." if g["reason"] else "."
    return [f"  Provider account: {provider_text(a)}; the gateway: {g['status']}{said}"]


def runs_lines(a: dict) -> list[str]:
    """The runs of the account and their cost, over 24 hours and 7 days and by principal this month."""
    if a["cost_24h_usd"] is None:  # a service account: the cost of a run is its model account's
        lines = [f"  Runs: {a['runs_24h']} in 24 hours, {a['runs_7d']} in 7 days."]
    else:
        lines = [
            f"  Runs: {a['runs_24h']} in 24 hours ({money(a['cost_24h_usd'])}), {a['runs_7d']} in 7 days"
            f" ({money(a['cost_7d_usd'])}, estimated from their usage)."
        ]
    held = ", ".join(f"run #{run['run_id']} ({run['project']}, {run['state']})" for run in a["held_by"]) or "no run"
    lines.append(f"  Held by: {held}.")
    lines += [
        f"  For {row['principal'] or '(gone)'}: {row['runs_month']} runs this month ({money(row['cost_month_usd'])}), "
        f"{row['runs_7d']} in 7 days ({money(row['cost_7d_usd'])})."
        for row in a.get("cost_by_principal") or []
    ]
    return lines


def account_lines(a: dict) -> list[str]:
    """An account as ``account show`` prints it."""
    reported = f", reported {when(a['last_usage_at'])} UTC" if a["last_usage_at"] else ""
    lines = [f"Account {a['name']} ({a['env_var']}): {state_text(a)}.", *gateway_lines(a)]
    if a.get("auth") == "oauth":
        lines.append("  Signed in with OAuth: the hub keeps its tokens sealed; renew with `account oauth-login`.")
    where = (
        f"pools: {', '.join(a['pools'])}" if a.get("pools") else f"workers: {', '.join(a['workers']) or 'any of yours'}"
    )
    lines.append(f"  Projects: {', '.join(a['projects']) or '-'}; {where}.")
    lines.append(f"  Expires: {expiry(a, datetime.now(UTC))[0]}.")
    lines.append(f"  Weight {a['weight']}, priority {a['priority']}; last taken {when(a['last_leased_at'])} UTC.")
    lines.append(f"  Last usage: {usage_text(a['last_usage'])}{reported}.")
    if windows := windows_text(a["last_usage"]):
        lines.append(f"  Windows: {windows}.")
    if a.get("weekly_budget_usd") is not None:
        start = f", weeks from {when(a['weekly_reset_at'])} UTC" if a.get("weekly_reset_at") else ", weeks from Monday"
        lines.append(f"  Weekly budget: {money(a['weekly_budget_usd'])}{start}.")
    return lines + runs_lines(a)
