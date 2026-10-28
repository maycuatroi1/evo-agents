"""The numbers and gates of the KG optimize loop (``evo_agents.hub.server.kg_optimize``), without the database: what a
measure of the graph holds, how far a figure may move before the loop calls it a regression, whether search by
meaning may be on (criterion a12 of plan kg-retrieval-quality), which weight of the asked column ranks best on the
tune split, and the report the owner gets at the end of a run.

A measure (``Measure``) is the holdout eval of one build asked twice, by words (BM25, the asked column at the weight in
effect) and by words and meaning fused (None without vectors or the model), with the tune split asked at each
candidate weight of the asked column. The noise of hit@5 is two standard errors of a proportion over the questions
asked, at least NOISE_FLOOR_POINTS; the noise of the tokens per hit is the same share of their value, at least
TOKEN_NOISE_FLOOR: an eval of a few dozen questions moves by more than a point between two builds of the same sources.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

NOISE_FLOOR_POINTS = 1.0  # hit@5, in percentage points: a change under this is noise however many questions
TOKEN_NOISE_FLOOR = 0.05  # the tokens per hit: a change under this share is noise
NOISE_SIGMAS = 2.0  # standard errors the noise spans
SEMANTIC_GAIN_POINTS = 5.0  # a12: hit@5 by meaning and words at least this far above words alone
SEMANTIC_P95_MS = 1000.0  # a12: kg_search on the hub answers within this at the 95th percentile
WEIGHTS = (0.0, 0.5, 1.0, 2.0)  # the weights of the asked column the loop tries on the tune split
MIN_TUNE_QUESTIONS = 30  # fewer questions on the tune split keep the weight as it is
MIN_NEW_LABELS = 20  # fewer labels since the weight was last tuned keep it as it is

ON, OFF, KEEP, UNAVAILABLE = "on", "off", "keep", "unavailable"  # what the semantic gate says


def figures(row: Mapping | None) -> dict | None:
    """The figures of an eval's kg_search row (``kg_health_eval.score``'s methods.kg_search) the loop compares: n,
    hit@5 in percent, tokens per hit and per answer file, p95 in ms; None without a row that asked anything."""
    if not isinstance(row, Mapping) or not isinstance(row.get("n"), int) or row["n"] <= 0:
        return None

    def number(key: str, scale: float = 1.0) -> float | None:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return round(float(value) * scale, 4)

    return {
        "n": row["n"],
        "hit@5": number("hit@5", 100.0),
        "tokens_per_hit": number("tokens_per_hit"),
        "tokens_per_file": number("tokens_per_file"),
        "p95_ms": number("p95_ms"),
    }


def hit_noise(found: Mapping) -> float:
    """The noise of ``found``'s hit@5 (``figures``), in percentage points."""
    n, hit = found.get("n") or 0, found.get("hit@5")
    if not n or hit is None:
        return NOISE_FLOOR_POINTS
    p = min(max(hit / 100.0, 0.0), 1.0)
    return round(max(NOISE_FLOOR_POINTS, NOISE_SIGMAS * 100.0 * math.sqrt(p * (1 - p) / n)), 2)


def token_noise(found: Mapping) -> float:
    """The noise of ``found``'s tokens per hit, as a share of them: the relative standard error of hit@5, which they
    are divided by, at least TOKEN_NOISE_FLOOR."""
    n, hit = found.get("n") or 0, found.get("hit@5")
    if not n or not hit:
        return TOKEN_NOISE_FLOOR
    p = min(max(hit / 100.0, 1e-9), 1.0)
    return round(max(TOKEN_NOISE_FLOOR, NOISE_SIGMAS * math.sqrt((1 - p) / (p * n))), 4)


def regressions(before: Mapping | None, after: Mapping | None) -> list[str]:
    """Why ``after`` is worse than ``before`` (both ``figures`` of the holdout by the mode in effect) beyond the
    noise of ``before``: hit@5 down, or the tokens per hit up; [] when it is not, or when either is missing."""
    if not before or not after:
        return []
    found = []
    hit_b, hit_a = before.get("hit@5"), after.get("hit@5")
    if hit_b is not None and hit_a is not None:
        noise = hit_noise(before)
        if hit_a < hit_b - noise:
            found.append(f"hit@5 fell from {hit_b:g}% to {hit_a:g}%, beyond the noise of {noise:g} points")
    tok_b, tok_a = before.get("tokens_per_hit"), after.get("tokens_per_hit")
    if tok_b and tok_a is not None:
        share = token_noise(before)
        if tok_a > tok_b * (1 + share):
            found.append(
                f"tokens per hit rose from {tok_b:g} to {tok_a:g}, beyond the noise of {round(share * 100, 1):g}%"
            )
    return found


def semantic_gate(words: Mapping | None, meaning: Mapping | None, mode: str, why: str | None = None) -> dict:
    """Whether search by meaning may be on (a12), from the holdout of one build by ``words`` and by ``meaning``
    (``figures``; None when it could not be asked, ``why``), the project's ``mode`` now: {decision (on, off, keep,
    unavailable), mode (the mode to set), reason, gain (points), and the figures}. ``on`` needs hit@5 at least
    SEMANTIC_GAIN_POINTS above words alone, tokens per answer file not up and p95 under SEMANTIC_P95_MS; a project
    whose mode is on and fails it goes back to ``index``, its builds keeping the vectors to measure again."""
    shown = {"words": dict(words) if words else None, "meaning": dict(meaning) if meaning else None}
    if not words or not meaning:
        reason = why or "the holdout could not be asked by meaning"
        return {"decision": UNAVAILABLE, "mode": mode, "reason": reason, "gain": None, **shown}
    hit_w, hit_m = words.get("hit@5"), meaning.get("hit@5")
    if hit_w is None or hit_m is None:
        return {"decision": UNAVAILABLE, "mode": mode, "reason": "the holdout has no hit@5", "gain": None, **shown}
    gain = round(hit_m - hit_w, 2)
    failed = []
    if gain < SEMANTIC_GAIN_POINTS:
        failed.append(f"hit@5 {hit_m:g}% against {hit_w:g}% by words, {gain:+g} points of the {SEMANTIC_GAIN_POINTS:g}")
    tok_w, tok_m = words.get("tokens_per_file"), meaning.get("tokens_per_file")
    if tok_w is not None and tok_m is not None and tok_m > tok_w:
        failed.append(f"tokens per answer file {tok_m:g} against {tok_w:g}")
    p95 = meaning.get("p95_ms")
    if p95 is None or p95 >= SEMANTIC_P95_MS:
        failed.append(f"p95 {p95:g} ms, not under {SEMANTIC_P95_MS:g}" if p95 is not None else "no p95")
    if not failed:
        reason = f"hit@5 {hit_m:g}% against {hit_w:g}% by words ({gain:+g} points), p95 {p95:g} ms"
        return {"decision": ON, "mode": "on", "reason": reason, "gain": gain, **shown}
    decision, to = (OFF, "index") if mode == "on" else (KEEP, mode)
    return {"decision": decision, "mode": to, "reason": "; ".join(failed), "gain": gain, **shown}


def best_weight(tuned: Mapping[str, Mapping | None], current: float, new_labels: int) -> dict:
    """The weight of the asked column to leave in effect, from the tune split asked at each weight (``tuned``, the
    weight as text: ``figures``): the best hit@5, then the fewest tokens per hit; ``current`` on a tie within the
    noise, with fewer than MIN_TUNE_QUESTIONS questions or fewer than MIN_NEW_LABELS ``new_labels``. {weight, changed,
    reason, tried: {weight: hit@5}}."""
    tried = {key: (found or {}).get("hit@5") for key, found in tuned.items()}
    kept = {"weight": current, "changed": False, "tried": tried}
    if new_labels < MIN_NEW_LABELS:
        return {**kept, "reason": f"{new_labels} new labels, fewer than {MIN_NEW_LABELS}: the weight stays"}
    rows = [(float(key), found) for key, found in tuned.items() if found and found.get("hit@5") is not None]
    if not rows or min(found["n"] for _, found in rows) < MIN_TUNE_QUESTIONS:
        return {**kept, "reason": f"fewer than {MIN_TUNE_QUESTIONS} tune questions: the weight stays"}
    now = next((found for weight, found in rows if math.isclose(weight, current)), None)
    weight, best = max(rows, key=lambda item: (item[1]["hit@5"], -(item[1].get("tokens_per_hit") or 0.0)))
    if now is None or math.isclose(weight, current) or best["hit@5"] - now["hit@5"] < hit_noise(now):
        return {**kept, "reason": f"no weight ranks the tune split better than {current:g} beyond the noise"}
    reason = f"hit@5 on the tune split {best['hit@5']:g}% at {weight:g} against {now['hit@5']:g}% at {current:g}"
    return {"weight": weight, "changed": True, "tried": tried, "reason": reason}


def _figure_text(found: Mapping | None) -> str:
    if not found:
        return "not measured"
    parts = [f"hit@5 {found.get('hit@5')}%", f"tokens per hit {found.get('tokens_per_hit')}"]
    parts += [f"tokens per answer file {found.get('tokens_per_file')}", f"p95 {found.get('p95_ms')} ms"]
    return ", ".join(parts) + f" ({found.get('n')} questions)"


def summary(project: str, body: Mapping, state: str) -> str:
    """The first line of the report: what the run came to."""
    before = (body.get("before") or {}).get("holdout")
    after = (body.get("after") or {}).get("holdout")
    if state == "reverted":
        ended = "reverted after a regression"
    elif state == "failed":
        ended = "failed"
    elif state in ("measuring", "running", "after"):
        ended = f"in flight ({state})"
    else:
        ended = "kept"
    hits = ""
    if before and after:
        hits = f": hit@5 {before.get('hit@5')}% to {after.get('hit@5')}%"
    return f"KG optimize of {project} {ended}{hits}"


def report_text(project: str, run_id: int, body: Mapping, state: str) -> str:
    """The report the owner gets (markdown): the figures before and after, click_rate and the new labels, the
    decision on search by meaning with its figures, the weight of the asked column, the decisions applied and undone,
    the pull requests merged or reverted, and what regressed."""
    before, after = body.get("before") or {}, body.get("after") or {}
    lines = [f"**{summary(project, body, state)}** (KG optimize run #{run_id}).", ""]
    lines.append(f"- Before (build {before.get('build_id')}): {_figure_text(before.get('holdout'))}.")
    lines.append(f"- After (build {after.get('build_id')}): {_figure_text(after.get('holdout'))}.")
    review_b, review_a = before.get("review_open"), after.get("review_open")
    if review_b is not None or review_a is not None:
        lines.append(f"- Open review items: {review_b} before, {review_a} after.")
    coverage = before.get("coverage")
    if isinstance(coverage, Mapping) and coverage:
        lines.append("- Coverage: " + ", ".join(f"{key} {value}" for key, value in sorted(coverage.items())) + ".")
    sessions = before.get("sessions") or {}
    lines.append(
        f"- Real sessions: click_rate {sessions.get('click_rate')}, {sessions.get('searches', 0)} searches, "
        f"{sessions.get('new_labels', 0)} new labels."
    )
    gate = body.get("semantic") or {}
    if gate:
        lines.append(
            f"- Search by meaning: {gate.get('decision')} (mode {gate.get('mode')}): {gate.get('reason')}. "
            f"By words: {_figure_text(gate.get('words'))}; by meaning: {_figure_text(gate.get('meaning'))}."
        )
    asked = body.get("asked") or {}
    if asked:
        lines.append(f"- Asked column weight {asked.get('weight')}: {asked.get('reason')}.")
    decisions = body.get("decisions") or {}
    numbers = decisions.get("numbers") or []
    shown = ", ".join(f"#{n}" for n in numbers) or "none"
    lines.append(f"- Graph decisions applied: {len(numbers)} ({shown}); {decisions.get('why') or 'review queue'}.")
    pulls = body.get("pulls") or []
    if pulls:
        listed = "; ".join(
            f"{item.get('repo')} {item.get('pr_url') or item.get('branch')}: {item.get('state')}" for item in pulls
        )
        lines.append(f"- Code changes: {listed}.")
    else:
        lines.append("- Code changes: none.")
    regressed = body.get("regressions") or []
    if regressed:
        lines.append("- Regression: " + "; ".join(regressed) + ".")
        undone = body.get("undone") or {}
        lines.append(
            f"- Undone: decisions {', '.join(f'#{n}' for n in undone.get('decisions') or []) or 'none'}; "
            f"revert proposals {', '.join(f'#{n}' for n in undone.get('reverts') or []) or 'none'}; "
            f"asked weight back to {undone.get('asked_weight', 'the previous one')}; "
            f"search by meaning back to {undone.get('semantic', 'its previous mode')}."
        )
    notes = body.get("notes") or []
    if notes:
        lines.append("- Notes: " + "; ".join(str(note) for note in notes) + ".")
    lines.append("- Rerank by Jev: off; the loop never turns it on.")
    return "\n".join(lines)


def prompt_lines(run_id: int, body: Mapping) -> list[str]:
    """What the review run of a KG optimize run reads of it: the figures the hub measured before, its decisions, and
    what the agent does: propose fixes of the KG code with evidence, which the Builder makes and the Judge checks."""
    before = body.get("before") or {}
    gate = body.get("semantic") or {}
    asked = body.get("asked") or {}
    return [
        "",
        f"# KG optimize run #{run_id}",
        "The hub measured the project's knowledge graph before this run; after it ends, the hub applies the fixes of "
        "the review queue as graph decisions (origin kg-optimize), rebuilds the graph and measures again, undoing "
        "what made the holdout worse.",
        f"- Holdout before (build {before.get('build_id')}): {_figure_text(before.get('holdout'))}.",
        f"- Open review items: {before.get('review_open')}; real sessions: {before.get('sessions')}.",
        f"- Search by meaning: {gate.get('decision')}: {gate.get('reason')}.",
        f"- Asked column weight: {asked.get('weight')}: {asked.get('reason')}.",
        "Look through the kg_health lens first: find what makes kg_search miss or spend tokens on the holdout and in "
        "real sessions, and propose fixes of the KG code (evo_agents/kg, the hub's kg build) with evidence. Do not "
        "turn on Jev's rerank. A fix that touches a protected path stays an open pull request.",
    ]
