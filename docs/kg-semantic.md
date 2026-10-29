# Search by meaning: `kg semantic`

`kg_search` ranks by words (BM25 over the full-text index, [docs/kg.md](kg.md#search)). A project can also have it rank
by meaning: each row of the index that has a body gets the vector of a multilingual embedding model, the query gets
one too, and the two rankings are fused. The model runs on the machine that builds the graph, the hub's worker or a
member's machine, never through a third party's API unless the API backend below is turned on. Code is `evo_agents/kg/semantic.py`.

It is off by default, and a project turns it on only when its own question set says it helps. The hub's KG optimize
loop turns it on for a project only when the holdout part of its set gives hit@5 at least 5 points above the search
by words, tokens per answer file no higher, and a hub p95 under one second. On the evo-agents questions (830, all
repos of the harness, graph of 20,462 rows with a body) it does not clear that bar, so it stays off there:

| Split | Search | hit@5 | tokens per answer file | p95 ms (local, 8 cores) |
| --- | --- | --- | --- | --- |
| tune (600) | by words | 0.238 | 1,674 | 34 |
| tune (600) | fused | 0.238 | 1,655 | 163 |
| holdout (230) | by words | 0.178 | 2,281 | 31 |
| holdout (230) | fused | 0.187 | 2,246 | 165 |

Most of those questions name a plan step and are answered by the files its commits changed; the ranking by meaning
finds sections and steps that talk about the subject more often than the code, so it lifts MRR more than hit@5.

## Modes

| Mode | Builds | `kg_search` |
| --- | --- | --- |
| `off` (default) | write no vector, and drop the ones a graph had | by words |
| `index` | write the vectors | by words, unless the caller asks for both (`kg eval --search hybrid`) |
| `on` | write the vectors | by words and by meaning, fused |

`index` exists to measure: one build answers `kg eval --search bm25` and `--search hybrid`, so the two runs differ in
the ranking alone.

On this machine the mode is in `<kg home>/<project>/semantic.json`:

```sh
evo-agents kg semantic --project demo                         # the mode, the model, what the graph holds
evo-agents kg semantic --project demo --mode index --index    # write the vectors now (downloads the model)
evo-agents kg eval questions.jsonl --split tune --method kg_search --search bm25
evo-agents kg eval questions.jsonl --split tune --method kg_search --search hybrid
evo-agents kg semantic --project demo --mode on               # the next build keeps them, kg_search uses them
```

On the hub the mode is the project's row of `kg_semantic` (migration 0181), off without one; the build reads it
(`evo_agents/hub/kg_semantic.py`). No HTTP route sets it yet: the KG optimize loop writes it.

## What is embedded

Each row of the full-text index with a body: a node's own row (a section, a document, a commit, a pull request, a step,
a symbol with a docstring) and each passage of a long section or file, which starts with its document's name and the
headings above it. The text is the row's name, its path and its body, cut at 2,000 characters (`EMBED_CHARS`, about
the 512 tokens the model reads well). Rows without a body, most symbols and every directory, get none: BM25 finds them
by their names. `đ` is folded to `d` in the query as in the index.

The vectors are in the graph file, in `node_vec` and `node_vec_meta`, tables outside the store's schema version and
the build's content hash: a reader of before opens the file and ignores them, and a graph without them, or a reader
without the model, searches by words as it did. On the hub the build folds the vectors' digest and the mode into the
content hash it records, so a build whose graph is the same but whose vectors are not uploads its file. Each vector is
also kept in `vectors.sqlite` next to the graph, by model and digest of its text, so a build embeds only the rows
whose text changed: the first one of a large project embeds everything (20,462 rows, 10.6 M characters for the
evo-agents graph, 72 minutes on 8 CPU cores), the next ones a few rows.

## The fusion

Reciprocal rank fusion (Cormack, Clarke and Buettcher, 2009): a node scores `1 / (60 + its rank by words)` plus
`SEMANTIC_WEIGHT / (60 + its rank by meaning)`, the weight 0.5 (on the tune split, weights from 0.25 to 5 and `RRF_K` of
20 or 60 moved hit@5 by under a point; 0.5 gave the fewest tokens per answer file), over the nodes each list holds. The
ranking by meaning holds the 100 nodes nearest the query (`SEMANTIC_POOL`), a node at its best row, of the kinds asked,
rows of an audience only for a reader within it, and is weighed like the ranking by words (`search.weight`: background
down, named nodes up). The nodes whose id, alias or name is the query still come first.

## Install

The model packages are an optional extra, `semantic` (fastembed 0.9, which brings ONNX Runtime, tokenizers and
huggingface_hub). Without it, `index` and `on` write nothing, the build says why in a warning, and search stays by
words.

The model is BAAI/bge-m3 (568 M parameters, 1,024 dimensions, over 100 languages, the ONNX export of its Hugging Face
repository): 2.2 GB on disk, a peak of about 1.6 GB of memory in a process that loads it and embeds a query, one query
in about 30 ms on 8 cores, no GPU needed. Models are read from `EVO_KG_MODEL_DIR`, by default
`~/.cache/evo-agents/models`. A build downloads the model when it is missing; a search never does: it loads the model
from that directory only, and searches by words when it is not there.

On a member's machine or a worker:

```sh
uv tool install 'evo-ak[worker,semantic]'      # or: pip install 'evo-ak[semantic]'
export EVO_KG_MODEL_DIR=~/.cache/evo-agents/models
evo-agents kg semantic --project demo --mode index --index    # downloads the model once, then embeds
```

On the hub, install the extra next to `hub-server` in the image or the virtualenv the hub and its kg worker run
(`pip install 'evo-ak[hub-server,semantic]'`), give both processes the same `EVO_KG_MODEL_DIR` on a volume that
outlives a deploy, and download the model once before the first build turns a project on, so no build waits on it:

```sh
EVO_KG_MODEL_DIR=/var/lib/evo-hub/models python -c \
  "from evo_agents.kg import semantic; semantic.embedder('BAAI/bge-m3', download=True)"
```

The hub process loads the model on the first search of a project whose graph is `on`, once a process, and keeps each
graph's vectors in memory (`MAX_MATRICES`, about 80 MB for 20,000 rows).

## The API backend (opt-in)

For a host with no GPU and no CPU time to spare, `EVO_KG_EMBEDDINGS=openrouter` makes `BAAI/bge-m3` come from
OpenRouter's embeddings API (`baai/bge-m3`, 1,024 dimensions, key in `OPENROUTER_API_KEY`) instead of fastembed. The
default stays the local model, the base dependencies are unchanged (the call uses the standard library; numpy comes
with the `semantic` extra), and **the texts of the rows leave the machine**, so do not use it on a graph that holds
private text. The model name, the cache and the stored vectors are the same as the local ones: the cache, keyed by the
digest of a row's text, reuses a vector across the snapshots of a repo.

Each call adds its tokens and cost to a ledger (`EVO_KG_EMBED_LEDGER`, default `<model dir>/openrouter-spend.jsonl`),
and the backend stops at 55 USD (`OPENROUTER_MAX_USD`). Price on 2026-10-29: 0.01 USD per million tokens, provider
Parasail. On a sample of 48 rows of this code base the API vectors and the local fastembed ones had a cosine of
0.99997 at the least and 0.999998 on average, and the same top 5 neighbours.
