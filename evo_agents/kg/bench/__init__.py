"""``evo-agents kg bench``: kg_search and kg_context on public code-retrieval benchmarks (docs/kg-bench.md).

- ``datasets``: the four sets at pinned revisions, fetched into a cache outside any repo, read into tasks.
- ``tasks``: a task as one `kg eval` question, the part of a repo (tune, holdout, test), dedup and sampling.
- ``graphs``: a clone per repo, one graph per repo and base commit in a kg home of the bench's own.
- ``run``: every task through the tools, no model called, into a summary JSON.
- ``compare``: two summaries paired task by task with a bootstrap, the gate, and ``show``'s checks.

The ranking code never imports this package (lint-imports, contract kg-ranking-not-eval): it holds the answers.
"""
