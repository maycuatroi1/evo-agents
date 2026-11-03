# Edges the graph may lack: `kg links`

The graph holds the edges its sources state (`parsed`) and the names it matched to one target (`resolved`). Where a
name matched several nodes or none, the build leaves the edge out and the hub's review queue lists the name
(`ambiguous_match`, `dangling_mention`). `kg links` trains a graph model on the parsed edges of a project's build,
scores the edges such a name could stand for, and proposes the likely ones. It never writes an edge: a proposal goes
to the hub as a `link` decision in state `proposed`, and only a writer or the Curator who accepts it puts the edge in
the next build. Code is `evo_agents/kg/links.py`.

```sh
pip install 'evo-agents[links]'                                   # numpy; the base install does not need it
evo-agents kg links propose --project demo                       # list what it would propose
evo-agents kg links propose --project demo --review review.json  # the queue from `hub kg review --json`
evo-agents kg links propose --project demo --hub --dry-run       # read the queue from the hub, send nothing
evo-agents kg links propose --project demo --hub --max 50        # send at most 50 proposals (kg_propose)
evo-agents kg links eval --set evo-agents=evo-agents --bench swe-explore,mulocbench,contextbench --graphs 4 \
  --out bench/results/m3-links.json
evo-agents kg bench show bench/results/m3-links.json --precision-min 0.8
```

## The model

DistMult, a knowledge graph embedding: each node and each relation is a vector of 64 numbers, and an edge (h, r, t)
scores the sum of their elementwise product; its probability is the sigmoid of that score. It trains on CPU with numpy
alone, 20 epochs with Adam, on the parsed edges only, never on resolved ones, which can be wrong. A negative is the
true edge with its head or its tail swapped for another node of the same kind, weighed by how likely the model finds
it. `--model rotate` trains RotatE instead (complex vectors, each relation a rotation).

## Its threshold

Of the parsed edges, 10% are hidden from training for the test and 5% more for choosing the threshold; an edge whose
head or tail would be left with no training edge stays in training. Each hidden edge is paired with a false one: the
same head and relation, a tail of the same kind that the head has no such edge to, and of the same name when such a
node exists, as an ambiguous name offers (8,224 of the 11,997 false edges of the evo-agents graph). The threshold is
the lowest probability at which the edges of the choosing share reach precision 0.85; the test share then gives the
precision and the recall at it. The 0.85 is a margin: chosen for 0.80, the test precision of the evo-agents graph
landed between 0.785 and 0.804 over four trials of model, size and epochs.

`kg links eval` writes this as a bench summary (`evo-kg-bench/1`) with one part, `holdout`, named as its `split`, so
`kg bench show` checks it without `--split` ([docs/kg-bench.md](kg-bench.md#show)). Precision and recall pool the
graphs of a set, each at its own threshold. The tune graphs are a sample drawn by the seed among those the bench cache
has built, one per repo.

bench/results/m3-links.json, DistMult, seed 20261103, four tune graphs per bench set, 8 CPU cores:

| Set | Graphs | Hidden edges | Precision | Recall | AUC (mean) | Train s |
| --- | --- | --- | --- | --- | --- | --- |
| evo-agents | 1 | 11,997 | 0.852 | 0.706 | 0.855 | 167 |
| swe-explore | 4 | 30,661 | 0.852 | 0.736 | 0.832 | 265 |
| mulocbench | 4 | 3,728 | 0.850 | 0.732 | 0.628 | 27 |
| contextbench | 4 | 40,211 | 0.850 | 0.911 | 0.898 | 368 |

Two mulocbench graphs are too small to learn from (43 and 133 hidden edges: AUC under 0.5, and no threshold for the
second); the set's numbers come from its two larger graphs, and its mean AUC is low for the same reason. RotatE on the
same graphs reached the same precision with lower recall (evo-agents 0.681, xarray 0.252 against 0.542, qutebrowser
0.351 against 0.643), in up to twice the time (laravel 381 s against 191 s), so DistMult is the default.

## What it scores

- `ambiguous`: each name of an `ambiguous_match` item, and each `resolved` edge whose Symbol target shares its name
  with 2 to 25 others: every node of that name is a candidate target of the same relation.
- `dangling`: each name of a `dangling_mention` item: the files with its base name and the symbols with its last part
  (`mentions`).
- `test`: a test file (`test_x.py`, `x_test.go`, `x.test.ts`, ...) and the code file of its stem it has no edge to
  (`verifies`). The model scores only relations its graph has, so these wait for a graph with `verifies` edges.
- `doc`, with `--docs`: a document and the 3 code files of its repo the model ranks highest that it has no edge to.
  Off by default: no name backs them and no hidden edge measures them.

Of the candidates of one source, relation and name, the best is proposed when it reaches the threshold and leads the
next by 0.1. The review queue comes from the build when it has clusters, else from `--review` or the hub (`--hub`).
On the evo-agents build with the hub's queue (105 dangling and 138 ambiguous items) it gives 431 proposals of 152,089
candidates: 420 ambiguous and 11 dangling; `--docs` adds 427 documents.

## To the hub

`--hub` sends each proposal through the MCP tool `kg_propose` (kind `link`, the source, relation and target, a reason
with the probability and the rivals), with the decision revision it read; the session's or the run's credentials, the
writer role. A refusal (a node the hub's build lacks, a stale revision, a proposal already made) is listed and the
next proposal reads the revision again. The proposals wait in the hub's decisions; nothing changes the graph until one
is accepted.
