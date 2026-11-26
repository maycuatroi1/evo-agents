"""How ``evo-agents hub canvas`` shows what the hub answered: a canvas by cell, a claim whole, an edit, a piece of
evidence, a proposal, a baseline and an import, each as the lines a person reads."""

from __future__ import annotations

from evo_agents.clikit import Text
from evo_agents.hub.cli.base import when


def claim_line(claim: dict) -> str:
    archived = ", archived" if claim["archived"] else ""
    facts = f"[{claim['status']}, {claim['confidence']}, rev {claim['rev']}{archived}]"
    evidence = (
        f" ({claim['real_support_count']}/{claim['evidence_count']} real support)" if claim["evidence_count"] else ""
    )
    return f"{claim['code']} {facts}{evidence} {claim['text']}"


def canvas_view(canvas: dict) -> Text:
    archived = " (archived)" if canvas["archived_at"] else ""
    lines = [f"{canvas['slug']}: {canvas['name']}{archived}"]
    if canvas["description"]:
        lines.append(f"  {canvas['description']}")
    for cell in canvas["cells"]:
        lines.append(f"{cell['cell'].replace('_', ' ').capitalize()}:")
        lines.extend(f"  {claim_line(claim)}" for claim in cell["claims"])
        if not cell["claims"]:
            lines.append("  -")
    return Text(*lines)


def card_lines(card: dict | None) -> list[str]:
    if not card:
        return ["  no test card"]
    return [f"  test card {name}: {value}" for name, value in card.items() if value is not None]


def rev_line(rev: dict) -> str:
    author = rev["provenance"]["login"] or "hub"
    if rev["provenance"]["actor"] == "agent":
        author = f"an agent for {author} (run {rev['provenance']['run_id']})"
    fields = ", ".join(change["field"] for change in rev["changes"])
    reason = f" ({rev['reason']})" if rev["reason"] else ""
    return f"  rev {rev['rev']} {when(rev['created_at'])} {rev['change']} by {author}: {fields}{reason}"


def claim_view(claim: dict) -> Text:
    archived = ", archived" if claim["archived"] else ""
    lines = [
        f"{claim['code']} in {claim['canvas']}, cell {claim['cell']}: {claim['text']}",
        f"  status {claim['status']}, confidence {claim['confidence']}, rev {claim['rev']}{archived}",
        f"  delivery: {claim['delivery']}",
        *card_lines(claim["test_card"]),
        f"  use cases: {', '.join(claim['use_cases']) or '-'}",
        f"  signal keys: {', '.join(claim['signal_keys']) or '-'}",
        f"  problems: {', '.join(claim['problems']) or '-'}",
        f"  opportunities: {', '.join(claim.get('opportunities') or []) or '-'}",
        "Evidence:" if claim["evidence"] else "No evidence.",
    ]
    for item in claim["evidence"]:
        real = "real" if item["real"] else f"not real: {item['not_real_because']}"
        who = item["provenance"]["login"] or item["provenance"]["actor"]
        lines.append(f"  #{item['id']} {item['kind']} {item['direction']} ({real}), by {who}: {item['summary']}")
    if claim["hidden_evidence"]:
        lines.append(f"  and {claim['hidden_evidence']} above what you may read")
    lines.append("Revs:")
    lines.extend(rev_line(rev) for rev in claim["revs"])
    return Text(*lines)


def edit_view(done: dict) -> Text:
    written = set(done["written"])
    return Text(
        *(f"{claim_line(claim)}{'' if claim['code'] in written else ' (unchanged)'}" for claim in done["claims"])
    )


def evidence_view(item: dict) -> Text:
    real = "real" if item["real"] else f"not real: {item['not_real_because']}"
    withdrawn = f", withdrawn: {item['withdrawn_reason']}" if item["withdrawn_at"] else ""
    return Text(f"#{item['id']} {item['kind']} {item['direction']} ({real}{withdrawn}): {item['summary']}")


def proposal_line(item: dict) -> str:
    what = f"{item['claim']} at rev {item['base_rev']}" if item["kind"] == "change" else "new claim"
    if item["kind"] == "create" and item["claim"]:
        what = f"new claim, now {item['claim']}"
    who = item["provenance"]["login"] or item["provenance"]["actor"]
    if item["provenance"]["actor"] == "agent":
        who = f"an agent for {who} (run {item['provenance']['run_id']})"
    synthetic = ", synthetic" if item["synthetic"] else ""
    return f"#{item['id']} [{item['status']}{synthetic}] {what}, by {who}: {item['reason']}"


def proposal_lines(item: dict) -> list[str]:
    lines = [proposal_line(item)]
    lines.extend(f"  {change['field']}: {change['before']!r} -> {change['after']!r}" for change in item["changes"])
    if item["status"] == "accepted":
        lines.append(f"  accepted by {item['decided_by']} as rev {item['rev']} of {item['claim']}")
    elif item["status"] == "rejected":
        why = f": {item['decided_reason']}" if item["decided_reason"] else ""
        lines.append(f"  rejected by {item['decided_by']}{why}")
    return lines


def proposal_view(item: dict) -> Text:
    return Text(*proposal_lines(item))


def baseline_view(found: dict) -> Text:
    week = f" {found['week']}" if found.get("week") else ""
    lines = [f"Baseline {found['number']} of {found['canvas']} ({found['kind']}{week}): {found['status']}"]
    for item in found["items"]:
        asked = f"{item['from_status']} -> {item['to_status']}" if item["to_status"] else f"overdue {item['deadline']}"
        said = f"{item['decision']} by {item['decided_by']}" if item["decision"] else "waiting"
        may = f"; you may: {', '.join(item['decisions'])}" if item["decisions"] else ""
        lines.append(f"  {item['claim']} [{item['kind']}] {asked}: {said}{may}")
    return Text(*lines)


def brainstorm_view(done: dict) -> Text:
    return Text(f"Run #{done['run_id']}: {done['title']}. It proposes; see `hub canvas proposal list` when it ends.")


def import_view(done: dict) -> Text:
    made = " (a new canvas)" if done["canvas_created"] else ""
    lead = "Would import" if done["dry_run"] else "Imported"
    lines = [
        f"{lead} canvas {done['canvas']}{made}: {len(done['created'])} new, {len(done['edited'])} changed, "
        f"{len(done['unchanged'])} unchanged, {done['evidence']} pieces of evidence."
    ]
    lines.extend(f"  {name}: {', '.join(done[name])}" for name in ("created", "edited") if done[name])
    for problem in done["problems"]:
        claim = f" {problem['claim']}" if problem["claim"] else ""
        lines.append(f"  {problem['level']}{claim}: {problem['message']}")
    return Text(*lines)
