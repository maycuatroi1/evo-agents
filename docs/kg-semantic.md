# Search by meaning: `kg semantic`

`kg_search` ranks by words (BM25 over the full-text index, [docs/kg.md](kg.md#search)). A project can also have it rank
by meaning: each row of the index that has a body gets the vector of a multilingual embedding model, the query gets
one too, and the two rankings are fused. The model runs on the machine that builds the graph, the hub's worker or a
member's machine, never through a third party's API. Code is `evo_agents/kg/semantic.py`.

It is off by default, and a project turns it on only when its own question set says it helps: on the evo-agents
questions it did not (below), so the hub's KG optimize loop turns it on for a project only when the holdout part of
its set gives hit@5 at least 5 points above the search by words, tokens per answer file no higher, and a hub p95
under one second.

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

On the hub the mode is the project's row of `kg_semantic` (migration 0180), off without one; the build reads it
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
evo-agents graph, about 45 minutes on 8 CPU cores), the next ones a few rows.

## The fusion

Reciprocal rank fusion (Cormack, Clarke and Buettcher, 2009): a node scores `1 / (60 + its rank by words)` plus
`SEMANTIC_WEIGHT / (60 + its rank by meaning)`, over the nodes each list holds. The ranking by meaning holds the 100
nodes nearest the query (`SEMANTIC_POOL`), a node at its best row, of the kinds asked, rows of an audience only for a
reader within it, and is weighed like the ranking by words (`search.weight`: background down, named nodes up). The
nodes whose id, alias or name is the query still come first.

## Install

The model packages are an optional extra, `semantic` (fastembed 0.9, which brings ONNX Runtime, tokenizers and
huggingface_hub). Without it, `index` and `on` write nothing, the build says why in a warning, and search stays by
words.

The model is BAAI/bge-m3 (568 M parameters, 1,024 dimensions, over 100 languages, the ONNX export of its Hugging Face
repository): about 2.3 GB on disk and some 2.5 GB of memory once loaded, one query in about 30 ms on 8 cores, no GPU
needed. Models are read from `EVO_KG_MODEL_DIR`, by default `~/.cache/evo-agents/models`. A build downloads the model
when it is missing; a search never does: it loads the model from that directory only, and searches by words when it
is not there.

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
