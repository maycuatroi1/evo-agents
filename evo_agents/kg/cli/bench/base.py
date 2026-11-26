"""What the ``evo-agents kg bench`` commands share: the arguments that select the tasks of a build or a run
(:data:`SELECTION`), the bench cache (:data:`CACHE`) and the argument of a share from 0 to 1 (:func:`ratio`)."""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg
from evo_agents.kg.bench import graphs, run
from evo_agents.kg.bench.tasks import PARTS

CACHE = Arg("--cache", help="bench cache, outside any repo")


def _ratio(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"{value} is not within 0 to 1")
    return value


def ratio(flag: str, text: str) -> Arg:
    """The option ``flag`` of a share from 0 to 1."""
    return Arg(flag, type=_ratio, metavar="R", help=text)


SELECTION = (
    Arg(
        "sets",
        nargs="*",
        help="code benches (default: all four), the non-code sets (musique, longmemeval, memoryagentbench, medical, "
        "literature, music, customers) or noncode for the seven (docs/kg-bench.md)",
    ),
    Arg("--cache", help="bench cache, outside any repo (default $EVO_KG_BENCH_CACHE or ~/.cache/...)"),
    Arg("--part", action="append", choices=PARTS, help="only this part (repeatable; default tune and holdout)"),
    Arg("--sealed", action="store_true", help="also read the sealed test part"),
    Arg("--sample", type=int, metavar="N", help="a fixed sample of N tasks per set, stratified by repo"),
    Arg("--seed", type=int, default=0, help="seed of the sample (default 0)"),
    Arg(
        "--max-hours",
        type=float,
        default=run.MAX_HOURS,
        help=f"sample a set whose graphs would take longer to build (default {run.MAX_HOURS:g}; 0: never)",
    ),
    Arg(
        "--git-url",
        default=graphs.GITHUB_URL,
        help="where repos are cloned from, {repo} standing for OWNER/NAME (default GitHub)",
    ),
    Arg(
        "--cache-gb",
        type=float,
        default=graphs.CAP_GB,
        help=f"graphs kept at most, the least recently used evicted past it (default {graphs.CAP_GB:g}; 0: no cap)",
    ),
    Arg(
        "--reserve-gb",
        type=float,
        help=f"free disk a build leaves, else the run stops before it (default {graphs.RESERVE_GB:g}, a tenth of a "
        "smaller disk)",
    ),
    Arg("--verbose", action="store_true", help="progress lines on stderr even with --json"),
    Arg(
        "--strategy",
        metavar="NAME",
        help="corpus sets: a graph of its own built with this kg strategy applied, its entities step included",
    ),
    Arg(
        "--entities",
        metavar="FILE",
        help="with --strategy: a json object of jobs, max_calls, max_tokens, max_cost set on its entities mapping",
    ),
)
