"""What ``hub run show`` and ``hub run list`` say a run spent: Run's ``spend``, the hub's ledger of its usage reports
(``evo_agents.hub.run_spend``), as its cost and tokens, then each model's."""

from __future__ import annotations

SPEND_TOKENS = (("input", "in"), ("cache_read", "cache read"), ("cache_write", "cache write"), ("output", "out"))


def usd(amount) -> str:
    """An amount in dollars: four decimals under a cent, else two; a dash for none."""
    return "-" if amount is None else f"${amount:.4f}" if 0 < amount < 0.01 else f"${amount:.2f}"


def cost_of(spend: dict) -> str:
    """A cost and how it came: reported by the runtime, estimated at list prices, or both."""
    reported, estimated = spend.get("reported_usd"), spend.get("estimated_usd")
    if reported is not None and estimated:
        return f"{usd(spend.get('cost_usd'))} ({usd(reported)} reported, {usd(estimated)} estimated)"
    if reported is None and estimated is not None:
        return f"{usd(estimated)} estimated at list prices"
    return usd(spend.get("cost_usd"))


def spend_lines(spend) -> list[tuple[str, str]]:
    """The lines of ``run show`` for what the run spent itself: its cost and tokens, then each model's."""
    if not isinstance(spend, dict):
        return []
    tokens = spend.get("tokens") or {}
    parts = ", ".join(f"{name} {tokens.get(key, 0):,}" for key, name in SPEND_TOKENS)
    total = f"{tokens.get('total', 0):,} tokens ({parts}), in {spend.get('sessions')} session(s)"
    lines = [("spend", f"{cost_of(spend)}, {total}")]
    for model in spend.get("models") or ():
        figures = model.get("tokens") or {}
        lines.append(("", f"{model.get('model') or '-'}: {cost_of(model)}, {figures.get('total', 0):,} tokens"))
    return lines
