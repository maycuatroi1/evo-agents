"""``evo-agents kg bench``: fetch, build, run, merge, compare and show the code and memory benches (docs/kg-bench.md)."""

from __future__ import annotations

from evo_agents.clikit import Group
from evo_agents.kg.cli.bench.curator import CURATOR
from evo_agents.kg.cli.bench.sets import BUILD, FETCH, RUN
from evo_agents.kg.cli.bench.summaries import COMPARE, MERGE, PROFILE, SHOW

BENCH = Group(
    "bench",
    "ContextBench, SWE-Explore, LocBench and MuLocBench on graphs of their repos (docs/kg-bench.md)",
    [FETCH, BUILD, RUN, COMPARE, MERGE, PROFILE, SHOW, CURATOR],
    dest="bench_command",
)
