"""``kg bench compare`` and ``kg bench show``: two summaries paired task by task, one summary checked.

Compare. For each set in both summaries, the tasks of the part asked for that both ran (same set, id and method, not
skipped) are paired on the set's primary metric; the paired differences are resampled BOOTSTRAP times with a fixed
seed. A set *regressed* when the mean difference is below minus 2 standard errors (the standard deviation of the
resampled means), and *improved* when the 95% interval of the resampled means lies above 0. ``--gate`` fails on any
regression; ``--target A,B --min-targets N`` fails unless N of the listed sets improved; ``--p95-ms`` fails when the
after summary's kg_search p95 over the part passes it; ``--tokens-drop R`` fails unless the mean tokens per paired
question fell by at least the ratio R.

Show. Prints a summary's numbers per set and part and checks it: ``--require`` names the sets that must have tasks
in ``--split``; a summary with a ``test`` part fails without ``--sealed``. The thresholds read keys of the part of a
required set (``benches.SET.parts.SPLIT``): ``abstain`` (share of questions without an answer the method abstained
on) for ``--abstain-min``, ``false_abstain`` (share of answerable ones it abstained on) for ``--false-abstain-max``,
``precision`` (mean line precision) for ``--precision-min``; ``--arms`` reads the summary's ``arms`` and
``--cost-max`` its ``cost.usd``. ``--curator-agrees N`` reads the summary's ``curator`` block (see below). A key the
summary lacks fails the check that reads it. A summary of one part only
(``kg links eval``) names it as its ``split``, which the checks read when ``--split`` is not given.

Curator block (``bench/results/curator.json``, ``curator-base.json``). A bench summary whose top-level ``benches`` and
``tasks`` are those of the config the Curator chose, plus ``curator.benches.SET`` with:

- ``chosen``: ``candidate`` (the name of its entry in ``candidates``), ``strategy`` (``name``, ``self_made``),
  ``settings`` (as decided), ``applied`` (the query keys the bench asked under) and ``unapplied`` (build keys a bench
  run cannot apply: the graphs were built with the defaults);
- ``inputs``: every file the Curator run read, each ``name``, ``path`` (relative to the file), ``sha256`` and ``part``
  (``none`` or ``tune``);
- ``candidates``: at least four configs besides the Curator's, each ``name``, ``kind``, ``search``, ``settings`` and
  ``parts.SPLIT`` with ``n``, ``primary`` and ``tasks`` ([{"id", "v", "tokens"}], the primary metric per task).

``--curator-agrees N`` fails unless N of the required sets have the Curator's config within SE_GATE standard errors of
the best candidate (the paired bootstrap of best minus Curator on the tasks every candidate ran), and fails when an input
is a file of holdout or test (by its ``part``, a path that names either, or a SHA-256 that differs from the file).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg.bench.datasets import BenchError
from evo_agents.kg.bench.run import FORMAT
from evo_agents.kg.bench.tasks import HOLDOUT, TEST
from evo_agents.kg.evaluate import p95

BOOTSTRAP = 2000
SEED = 20261029
SE_GATE = 2.0


def load_summary(path: Path | str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BenchError(f"no summary {path}") from None
    except json.JSONDecodeError as exc:
        raise BenchError(f"{path} is not JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BenchError(f"{path} is not a bench summary ({FORMAT})")
    return data


def merge(summaries: list[dict], ledger: Path | str | None = None) -> dict:
    """One summary from several of the same run's search, extractor and settings, each holding other sets (a pass
    a set). The sets and tasks add up. With ``ledger`` (the API embeddings' spend, JSON lines) a hybrid summary
    names where its vectors came from and the cost carries what they cost."""
    first = summaries[0]
    names = [name for s in summaries for name in s["benches"]]
    if len(set(names)) != len(names):
        raise BenchError("a set is in two summaries: " + ", ".join(sorted({n for n in names if names.count(n) > 1})))
    for s in summaries[1:]:
        for key in ("extractor",):
            if s.get(key) != first.get(key):
                raise BenchError(f"summaries differ in {key}: {first.get(key)} and {s.get(key)}")
        if s["settings"].get("search") != first["settings"].get("search"):
            raise BenchError("summaries differ in search")
    out = {**first, "benches": {}, "tasks": [], "cost": {**first.get("cost", {})}}
    out["settings"] = {**first["settings"], "sets": names}
    for s in summaries:
        out["benches"].update(s["benches"])
        out["tasks"] += s["tasks"]
    if ledger is not None and first["settings"].get("search") == "hybrid":
        rows = [json.loads(line) for line in Path(ledger).read_text(encoding="utf-8").splitlines() if line.strip()]
        providers = sorted({r["provider"] for r in rows if r.get("provider")})
        out["settings"]["embeddings"] = {
            "backend": "openrouter",
            "model": "baai/bge-m3",
            "providers": providers,
            "tokens": sum(r.get("tokens", 0) for r in rows),
        }
        out["cost"] = {"usd": round(sum(r.get("usd", 0.0) for r in rows), 4), "model_calls": len(rows)}
    return out


def _records(summary: dict, bench: str, split: str) -> dict[str, dict]:
    method = summary["benches"][bench].get("method", "kg_search")
    return {
        r["id"]: r
        for r in summary.get("tasks", [])
        if r["bench"] == bench and r["part"] == split and r["method"] == method and r.get("skipped") is None
    }


@dataclass
class Paired:
    bench: str
    metric: str
    n: int
    before: float | None = None
    after: float | None = None
    mean: float = 0.0
    se: float = 0.0
    low: float = 0.0
    high: float = 0.0
    before_tokens: float = 0.0
    after_tokens: float = 0.0

    @property
    def regressed(self) -> bool:
        return self.n > 0 and self.mean < 0 and self.mean < -SE_GATE * self.se

    @property
    def improved(self) -> bool:
        return self.n > 0 and self.low > 0

    def to_json(self) -> dict:
        return {
            "bench": self.bench,
            "metric": self.metric,
            "n": self.n,
            "before": self.before,
            "after": self.after,
            "diff": round(self.mean, 4),
            "se": round(self.se, 4),
            "ci95": [round(self.low, 4), round(self.high, 4)],
            "regressed": self.regressed,
            "improved": self.improved,
        }


def bootstrap(diffs: list[float], resamples: int = BOOTSTRAP, seed: int = SEED) -> tuple[float, float, float, float]:
    """The mean of ``diffs``, the standard error and the 95% interval of the mean, by resampling with ``seed``."""
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0
    mean = sum(diffs) / n
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    centre = sum(means) / resamples
    se = math.sqrt(sum((m - centre) ** 2 for m in means) / max(resamples - 1, 1))
    return mean, se, means[int(0.025 * resamples)], means[min(resamples - 1, int(math.ceil(0.975 * resamples)) - 1)]


def pair(before: dict, after: dict, bench: str, split: str) -> Paired:
    metric = after["benches"][bench]["primary"]
    if before["benches"][bench]["primary"] != metric:
        raise BenchError(f"{bench}: the summaries score it on {before['benches'][bench]['primary']} and {metric}")
    a, b = _records(before, bench, split), _records(after, bench, split)
    ids = sorted(i for i in a.keys() & b.keys() if metric in a[i]["metrics"] and metric in b[i]["metrics"])
    if not ids:
        return Paired(bench, metric, 0)
    xs = [a[i]["metrics"][metric] for i in ids]
    ys = [b[i]["metrics"][metric] for i in ids]
    mean, se, low, high = bootstrap([y - x for x, y in zip(xs, ys, strict=True)])
    return Paired(
        bench,
        metric,
        len(ids),
        round(sum(xs) / len(xs), 4),
        round(sum(ys) / len(ys), 4),
        mean,
        se,
        low,
        high,
        sum(a[i]["tokens"] for i in ids) / len(ids),
        sum(b[i]["tokens"] for i in ids) / len(ids),
    )


@dataclass
class Verdict:
    rows: list[Paired]
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (name, passed, detail)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "sets": [r.to_json() for r in self.rows],
            "checks": [{"check": name, "ok": passed, "detail": detail} for name, passed, detail in self.checks],
        }

    def lines(self) -> list[str]:
        out = [f"{'set':<14} {'metric':<11} {'n':>5} {'before':>7} {'after':>7} {'diff':>8} {'se':>7}  ci95"]
        for r in self.rows:
            mark = " regressed" if r.regressed else " improved" if r.improved else ""
            out.append(
                f"{r.bench:<14} {r.metric:<11} {r.n:>5} {_num(r.before):>7} {_num(r.after):>7} {r.mean:>+8.4f} "
                f"{r.se:>7.4f}  [{r.low:+.4f}, {r.high:+.4f}]{mark}"
            )
        out += [f"{'ok' if passed else 'FAIL'}  {name}: {detail}" for name, passed, detail in self.checks]
        return out


def _num(value) -> str:
    return "-" if value is None else f"{value:.4f}"


def _p95_after(after: dict, split: str) -> float | None:
    ms = [
        r["ms"]
        for r in after.get("tasks", [])
        if r["part"] == split and r["method"] == "kg_search" and r.get("skipped") is None
    ]
    return round(p95(ms), 1) if ms else None


def compare(
    before: dict,
    after: dict,
    *,
    split: str = "holdout",
    gate: bool = False,
    targets: list[str] | None = None,
    min_targets: int | None = None,
    p95_ms: float | None = None,
    tokens_drop: float | None = None,
) -> Verdict:
    """The paired comparison of two summaries on ``split``, with the checks asked for (see the module)."""
    common = [b for b in after.get("benches", {}) if b in before.get("benches", {})]
    if not common:
        raise BenchError("the two summaries have no set in common")
    verdict = Verdict([pair(before, after, b, split) for b in common])
    if gate:
        down = [r.bench for r in verdict.rows if r.regressed]
        verdict.checks.append(
            (
                "gate",
                not down,
                f"dropped by over {SE_GATE:g} standard errors: {', '.join(down)}"
                if down
                else f"no set dropped by over {SE_GATE:g} standard errors",
            )
        )
    if targets:
        need = len(targets) if min_targets is None else min_targets
        up = [r.bench for r in verdict.rows if r.bench in targets and r.improved]
        verdict.checks.append(
            (
                "targets",
                len(up) >= need,
                f"{len(up)} of {', '.join(targets)} improved beyond the 95% interval "
                f"({', '.join(up) or 'none'}), {need} needed",
            )
        )
    if p95_ms is not None:
        found = _p95_after(after, split)
        verdict.checks.append(
            ("p95-ms", found is not None and found <= p95_ms, f"kg_search p95 {found} ms, at most {p95_ms:g}")
        )
    if tokens_drop is not None:
        paired = [r for r in verdict.rows if r.n]
        n = sum(r.n for r in paired)
        before_tokens = sum(r.before_tokens * r.n for r in paired) / n if n else 0.0
        after_tokens = sum(r.after_tokens * r.n for r in paired) / n if n else 0.0
        drop = 1 - after_tokens / before_tokens if before_tokens else 0.0
        verdict.checks.append(
            (
                "tokens-drop",
                n > 0 and drop >= tokens_drop,
                f"tokens per question {before_tokens:.0f} -> {after_tokens:.0f} ({drop:.1%}), at least {tokens_drop:.1%}",
            )
        )
    return verdict


# -- show ----------------------------------------------------------------------------------------------------------


def show_lines(summary: dict) -> list[str]:
    head = summary.get("evo_ak") or {}
    out = [
        f"bench summary of {summary.get('created_at')}: evo-ak {head.get('version')} ({(head.get('sha') or '-')[:12]}), "
        f"extractor {summary.get('extractor')}, search {summary.get('settings', {}).get('search')}, "
        f"seed {summary.get('settings', {}).get('seed')}",
        f"{'set':<14} {'part':<8} {'metric':<11} {'n':>5} {'primary':>8} {'tokens':>8} {'p95 ms':>8}",
    ]
    for bench, numbers in summary.get("benches", {}).items():
        for part, row in numbers.get("parts", {}).items():
            out.append(
                f"{bench:<14} {part:<8} {numbers.get('primary', '-'):<11} {row.get('n', 0):>5} "
                f"{_num(row.get('primary')):>8} {row.get('mean_tokens') or 0:>8.0f} {row.get('p95_ms') or 0:>8.1f}"
            )
        if numbers.get("sample"):
            s = numbers["sample"]
            out.append(f"{'':<14} sampled {s['size']} of {s['of']} (seed {s['seed']}): {s['why']}")
    return out


def _threshold(checks, parts: dict, key: str, flag: str, limit: float, at_least: bool) -> None:
    for bench, part in parts.items():
        value = part.get(key)
        if value is None:
            checks.append((flag, False, f"{bench} has no {key}"))
            continue
        passed = value >= limit if at_least else value <= limit
        checks.append((flag, passed, f"{bench} {key} {value} {'>=' if at_least else '<='} {limit:g}"))


def check(
    summary: dict,
    *,
    require: list[str] | None = None,
    split: str | None = None,
    sealed: bool = False,
    abstain_min: float | None = None,
    false_abstain_max: float | None = None,
    precision_min: float | None = None,
    arms: list[str] | None = None,
    cost_max: float | None = None,
    curator_agrees: int | None = None,
    base: Path | str | None = None,
) -> list[tuple[str, bool, str]]:
    """The checks ``show`` was asked for (see the module), each (name, passed, detail)."""
    checks: list[tuple[str, bool, str]] = []
    split = split or summary.get("split")
    benches = summary.get("benches", {})
    tested = sorted(b for b, n in benches.items() if TEST in n.get("parts", {}))
    if tested and not sealed:
        checks.append(("sealed", False, f"the test part is sealed, yet {', '.join(tested)} have it: pass --sealed"))
    for bench in require or []:
        parts = benches.get(bench, {}).get("parts", {})
        n = parts.get(split, {}).get("n", 0) if split else sum(p.get("n", 0) for p in parts.values())
        checks.append(("require", n > 0, f"{bench}: {n} tasks{f' in {split}' if split else ''}"))
    names = require or list(benches)
    picked = {b: benches[b]["parts"][split] for b in names if split and split in benches.get(b, {}).get("parts", {})}
    if not split and any(v is not None for v in (abstain_min, false_abstain_max, precision_min)):
        checks.append(("split", False, "the thresholds read one part: give --split"))
    else:
        for key, flag, limit, at_least in (
            ("abstain", "abstain-min", abstain_min, True),
            ("false_abstain", "false-abstain-max", false_abstain_max, False),
            ("precision", "precision-min", precision_min, True),
        ):
            if limit is not None:
                _threshold(checks, picked, key, flag, limit, at_least)
    if arms:
        have = set(summary.get("arms") or [])
        missing = [a for a in arms if a not in have]
        checks.append(("arms", not missing, f"missing arms: {', '.join(missing)}" if missing else "every arm ran"))
    if cost_max is not None:
        cost = (summary.get("cost") or {}).get("usd")
        checks.append(("cost-max", cost is not None and cost <= cost_max, f"cost {cost} USD, at most {cost_max:g}"))
    if curator_agrees is not None:
        checks += curator_checks(summary, require or [], split, curator_agrees, base)
    return checks


# -- the Curator's choice ------------------------------------------------------------------------------------------

CURATOR_MIN_CANDIDATES = 4
HELD_OUT = (HOLDOUT, TEST)
_HELD_OUT_WORD = re.compile(r"(?<![a-z])(holdout|test)(?![a-z])", re.IGNORECASE)


def held_out_input(entry: dict, base: Path | str | None = None) -> str | None:
    """Why the input file ``entry`` of a Curator run is one the run must not read (or None): its part is holdout or
    test, its name or path says so, or its SHA-256 is not that of the file it names."""
    if entry.get("part") in HELD_OUT:
        return f"part {entry['part']}"
    for key in ("name", "path"):
        found = _HELD_OUT_WORD.search(str(entry.get(key) or ""))
        if found:
            return f"{key} {entry[key]!r} names {found.group(1).lower()}"
    if not re.fullmatch(r"(sha256:)?[0-9a-f]{64}", str(entry.get("sha256") or "")):
        return "no SHA-256"
    path = Path(str(entry.get("path") or ""))
    if base is not None and not path.is_absolute() and (Path(base) / path).is_file():
        digest = hashlib.sha256((Path(base) / path).read_bytes()).hexdigest()
        if digest != str(entry["sha256"]).removeprefix("sha256:"):
            return f"SHA-256 of {path.as_posix()} differs from the one recorded"  # the same words on Windows
    return None


def _tasks_of(candidate: dict, split: str) -> dict[str, float]:
    part = (candidate.get("parts") or {}).get(split) or {}
    return {t["id"]: t["v"] for t in part.get("tasks", []) if isinstance(t, dict) and t.get("v") is not None}


def agreement(entry: dict, split: str) -> dict:
    """How far the Curator's config of one set is from the best candidate on ``split``: the tasks every candidate ran
    are paired, ``gap`` is best minus Curator (mean), ``se`` the standard error of that paired difference."""
    candidates = entry.get("candidates") or []
    chosen = (entry.get("chosen") or {}).get("candidate")
    named = {c.get("name"): c for c in candidates}
    if chosen not in named:
        return {"ok": False, "why": f"chosen candidate {chosen!r} is not among the candidates"}
    series = {name: _tasks_of(c, split) for name, c in named.items()}
    ids = sorted(set.intersection(*(set(v) for v in series.values())))
    if not ids:
        return {"ok": False, "why": f"no task in {split} that every candidate ran"}
    means = {name: sum(v[i] for i in ids) / len(ids) for name, v in series.items()}
    best = max(means, key=lambda n: (means[n], n == chosen))
    diffs = [series[best][i] - series[chosen][i] for i in ids]
    gap, se, _, _ = bootstrap(diffs)
    return {
        "ok": gap <= SE_GATE * se + 1e-12,
        "n": len(ids),
        "best": best,
        "best_primary": round(means[best], 4),
        "chosen": chosen,
        "chosen_primary": round(means[chosen], 4),
        "gap": round(gap, 4),
        "se": round(se, 4),
    }


def curator_checks(
    summary: dict, require: list[str], split: str | None, need: int, base: Path | str | None = None
) -> list[tuple[str, bool, str]]:
    """The checks of ``--curator-agrees`` (see the module)."""
    out: list[tuple[str, bool, str]] = []
    block = (summary.get("curator") or {}).get("benches") or {}
    split = split or (summary.get("curator") or {}).get("split") or HOLDOUT
    names = require or list(block)
    agreeing = []
    for bench in names:
        entry = block.get(bench)
        if not entry:
            out.append(("curator-agrees", False, f"{bench}: no Curator block"))
            continue
        bad = [f"{e.get('name')}: {why}" for e in entry.get("inputs") or [] if (why := held_out_input(e, base))]
        if not entry.get("inputs"):
            bad.append("no input list")
        out.append(
            ("curator-inputs", not bad, f"{bench}: " + ("; ".join(bad) if bad else "no input of holdout or test"))
        )
        chosen = (entry.get("chosen") or {}).get("candidate")
        extra = [c for c in entry.get("candidates") or [] if c.get("name") != chosen]
        out.append(
            (
                "curator-candidates",
                len(extra) >= CURATOR_MIN_CANDIDATES,
                f"{bench}: {len(extra)} candidates besides the Curator's, at least {CURATOR_MIN_CANDIDATES}",
            )
        )
        found = agreement(entry, split)
        if "why" in found:
            out.append(("curator-set", False, f"{bench}: {found['why']}"))
            continue
        if found["ok"]:
            agreeing.append(bench)
        out.append(
            (
                "curator-set",
                True,  # informational: only the count in curator-agrees decides
                f"{bench}: {'within' if found['ok'] else 'outside'} {SE_GATE:g} SE: Curator {found['chosen_primary']} ({found['chosen']}), best {found['best_primary']} "
                f"({found['best']}), gap {found['gap']:+.4f}, SE {found['se']:.4f}, n {found['n']}",
            )
        )
    detail = f"{len(agreeing)} of {len(names)} within {SE_GATE:g} SE of the best ({', '.join(agreeing) or 'none'})"
    out.append(("curator-agrees", len(agreeing) >= need, f"{detail}, {need} needed"))
    return out
