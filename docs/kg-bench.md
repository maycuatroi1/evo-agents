# Public code benches: `kg bench`

`evo-agents kg bench` scores the graph's retrieval on four public sets of real GitHub issues, each with the code a
fix needed: ContextBench, SWE-Explore, LocBench and MuLocBench. Each task is asked on a graph of its own repo at the
issue's base commit, the way `kg eval` asks a question (docs/kg-eval.md). No model is called, so a run costs only
the machine time it takes.

```sh
pip install 'evo-ak[bench]'                      # pyarrow, to read the parquet files of three of the sets
evo-agents kg bench fetch                         # the four sets, at their pinned revisions, into the cache
evo-agents kg bench build locbench                # the graphs LocBench's tune and holdout tasks need
evo-agents kg bench run --out results/base.json   # every set, parts tune and holdout
evo-agents kg bench compare results/base.json results/new.json --split holdout --gate
evo-agents kg bench show results/base.json --require contextbench,swe-explore,locbench,mulocbench --split holdout
```

## The sets

| Set | Source (Hugging Face, pinned) | Tasks | Primary metric |
|---|---|---|---|
| `contextbench` | `Contextbench/ContextBench`, `data/full.parquet` | 1136, 8 languages | F1 by line |
| `swe-explore` | `SWE-Explore-Bench/SWE-Explore-Bench`, `bench.final.public.jsonl`, issue and commit from SWE-bench Verified, Pro and Multilingual | 848 | F1 by line, 5 results |
| `locbench` | `czlll/Loc-Bench_V1`, `data/test-00000-of-00001.parquet` | 560, 165 Python repos | Acc@5 by file |
| `mulocbench` | `somethingone/MULocBench`, `all_data_card.jsonl` | 1040 with an answer, 46 repos | Acc@5 by file |
| a local set, such as `evo-agents` | a `kg eval` question set (`run --set evo-agents=FILE`) | yours | hit@5 |

The revision of every file is pinned in `evo_agents/kg/bench/datasets.py`, and `fetch` records each file's SHA-256 in
`fetched.json`. Changing a revision is a change of the bench, so make it in a commit of its own.

- **Question:** the issue text as the set gives it, verbatim. For MuLocBench this is the title, a blank line, then
  the body.
- **ContextBench answers:** its gold context, as files and line ranges.
- **SWE-Explore answers:** its core regions and core files. It scores 5 results, as its explorers return 5 regions.
- **LocBench answers:** the files the fix changes and the functions it edits. The changed lines come from the
  patch: a removed line, or for an insertion the line before it. The patch decides the answer and is never asked.
- **MuLocBench answers:** the files it locates (an added file is left out), their functions, and their changed
  lines.
- **The answers use bare paths.** A graph holds one repo, so a path matches whichever repo it is in.

**Licenses.** ContextBench names no license, and SWE-Explore is CC BY-NC-ND 4.0. No repo may hold their data, so the
cache is refused inside a git work tree. A summary holds task ids and scores, never the issue text, so it can be committed.

## Parts: tune, holdout, test

Each repo falls into one part, by the SHA-256 of its lowercased name. The part is the same in every set and on every
machine: half the repos go to `tune`, a quarter to `holdout`, the rest to `test`.

- A LocBench repo that the hash puts in `tune` goes to `holdout` instead, in every set. That way LocBench only scores
  repos ranking was never tuned on. The 165 names are pinned in `evo_agents/kg/bench/locbench_repos.py`, and `fetch`
  fails when LocBench names one the list lacks.
- `test` is sealed. `run` and `build` read it only with `--sealed`, and `show` fails on a summary that has it unless
  given `--sealed`. Keep sealed runs out of the summaries that later steps read.
- A local set keeps the parts `kg eval` gives it (by the hash of each question's id). It has no `test` part.

**Dedupe.** ContextBench and SWE-Explore both draw on SWE-bench, and LocBench and MuLocBench share repos. A task is
kept once, in the first set of this order: `swe-explore`, `locbench`, `mulocbench`, `contextbench`. The smaller sets
keep their tasks. Reading a set needs every set before it fetched, so the dedupe never depends on what a machine
happened to fetch. Each summary records how many tasks the dedupe dropped (`deduped`).

## Graphs

The cache (`--cache`, else `$EVO_KG_BENCH_CACHE`, else `~/.cache/evo-agents/kg-bench`) holds:

```
data/SET/raw/DATASET/REVISION/FILE   the files as downloaded
data/SET/tasks.jsonl                 the tasks read from them; fetched.json: revisions, SHA-256, what was left out
data/SET/questions.jsonl             the last run's tasks as a `kg eval` question set
repos/OWNER__NAME.git                a bare clone of each repo
kg/EXTRACTOR/home/                   the EVO_KG_HOME of the bench: a project per repo and base commit
kg/EXTRACTOR/memo/OWNER__NAME.sqlite the extraction memo of each repo, shared by its snapshots
kg/EXTRACTOR/builds.jsonl            how long each build took
```

- **Snapshot.** A snapshot is the base commit, read straight from the bare clone together with its history. The
  repo's pull requests are never read, because one of them is the fix.
- **No hub.** The builds and the runs set EVO_KG_HOME to the bench's own home. They never touch the hub, nor this
  machine's own kg home.
- **Reuse.** A graph is built once and reused while `EXTRACTOR` stays the same. `EXTRACTOR` is a digest of the
  store's schema, the version of each stage, graphify, the git connector's code and the search index's text. A
  release that only changes ranking searches the graphs it already has.
- **Memo.** Each repo has its own extraction memo, so a file unchanged between two snapshots is not read twice, and
  parallel builds of different repos never wait on one database.
- **Clones.** `--git-url` changes where repos are cloned from, with `{repo}` standing for `OWNER/NAME`. Use it for a
  mirror or for local copies.

## Run

`run` asks each selected task with `kg_search` (add `--method kg_context` or `--method grep` to run those too; the
first method is the primary one). It uses the same scoring as `kg eval --acc`: line scores within `--line-budget`
(500), Acc@1, 5 and 10 by file and by function, hit@5 and MRR.

**Searches.** `--search` picks the ranking, and can be given more than once, with one `--out` per search in the same
order. Each graph is built once and every search asks it before the next graph, so the arms compare on the same
graphs and tasks.

| Search | Ranks by |
|---|---|
| `graph` | the shipped ranking, as `kg eval --search graph`: weighted FTS5 BM25, kind priors, `asked`, the weak cut to 5 |
| `bm25` | `kg eval --search bm25`; on a bench graph, which holds no vectors, the same as `graph` |
| `plain` | plain BM25: every word of the issue but stop words, ORed, over names, text and paths of files, symbols, documents and sections; no priors, no mentions, no weak cut |
| `hybrid` | BM25 fused with bge-m3 by RRF; the vectors of each graph are indexed first (the `semantic` extra) |

`plain` is the BM25 row the published benches report, so the deviation from them reads the ranking alone.

**Room and time.** `--prune` deletes each snapshot's graph after its last task, and each repo's clone and memo after
its last snapshot, so a run needs room for the graphs in flight only. `--jobs N` builds and asks N repos at once, the
largest first, each in its own process.

**Sampling.** A set whose missing graphs would take longer than `--max-hours` (default 6) to build is sampled:

- The size is the share of tasks the time allows, at the mean build time measured so far (`builds.jsonl`).
- The sample takes the same share of every repo, ordered by `--seed`.
- `--sample N` takes N tasks per set outright.
- The summary names the sample: its size, what it was drawn from, the seed, why it was drawn, and its task ids.
- There is no estimate before any build has been timed. Build a few graphs first (`build --sample 20`), or pass
  `--sample`.

**The summary** (`--out`, format `evo-kg-bench/1`) holds:

- **`evo_ak`:** the version, and the commit of the checkout this ran from.
- **`extractor`:** the digest of the graphs.
- **`settings`:** the methods, search, budgets, parts, seed, sample, `max_hours` and `sealed`.
- **`cost`:** `usd` 0, since no model is called.
- **`benches.SET`:**
  - Its sources: `revision` (dataset to revision) and `fetched` (file SHA-256s).
  - What was asked: `primary`, `method`, `limit`, `tasks`, `deduped`, `asked` and `sample`.
  - `build`: built, reused, failed, and seconds.
  - `parts.PART`: `n`, `primary` (the mean of the primary metric), `mean_tokens`, `p95_ms`, `precision` (the mean
    line precision), `row` (the `kg eval` row) and `languages` (n and primary per language).
- **`tasks`:** one record per task and method, with the set, id, part, repo, language, metrics, tokens, ms, error,
  and why it was skipped. A task whose graph failed to build is recorded as skipped, with the build error.

## Compare

`compare BEFORE AFTER --split holdout` pairs the tasks both summaries ran, per set present in both, on the set's
primary metric. It resamples the paired differences 2000 times with a fixed seed.

- A set has **regressed** when the mean difference is below minus 2 standard errors.
- A set has **improved** when the 95% interval of the mean lies above zero.

The checks below make the exit code non-zero when they fail. Without any of them, `compare` only reports.

| Flag | Fails when |
|---|---|
| `--gate` | any set regressed |
| `--target A,B --min-targets N` | fewer than N of the listed sets improved (default: all of them) |
| `--p95-ms MS` | kg_search's p95 latency in AFTER, over the part, is above MS |
| `--tokens-drop R` | mean tokens per paired question did not fall by at least the ratio R |

## Show

`show SUMMARY` prints the numbers per set and part, then runs the checks it is given:

- `--require A,B` fails unless each named set has tasks in `--split`.
- `--abstain-min`, `--false-abstain-max` and `--precision-min` read the keys `abstain`, `false_abstain` and
  `precision` of `benches.SET.parts.SPLIT` for each required set, or each set when none is required.
- `--arms A,B` reads the summary's `arms` list.
- `--cost-max USD` reads `cost.usd`.
- A key the summary lacks fails the check that reads it. A later step that adds abstaining or arms writes these keys.

## On a laptop, on a worker

**On a laptop**, run one set and a sample, so the graphs stay small:

```sh
evo-agents kg bench fetch
evo-agents kg bench run locbench --sample 40 --seed 1 --out /tmp/loc.json
```

**On a worker**, run everything. Point `--cache` at a disk with room, and keep it between runs, since the graphs are
reused:

```sh
export EVO_KG_BENCH_CACHE=/data/kg-bench
evo-agents kg bench fetch
evo-agents kg bench build --sample 20            # times a few builds, so --max-hours can estimate
evo-agents kg bench run --out bench/results/base.json --json > /dev/null
```

**Cache size.** The data files take about 60 MB: ContextBench 26 MB, SWE-Explore 14 MB plus about 12 MB of SWE-bench
issues, LocBench a few MB, and MuLocBench 4.3 MB. The clones and graphs take most of the room:

- A bare clone is fetched once per repo. Small repos take 5 to 15 MB; large ones (django, transformers, cpython)
  take hundreds of MB to a few GB.
- A graph is built per repo and base commit, and nearly every task has its own commit. Measured on 2026-10-29: 14 to
  34 MB for a small Python repo (thefuck, requests, flask).
- A full run of all four sets builds about 3500 graphs. Expect hundreds of GB, which is why `--max-hours` samples.

**Build time.** Measured on 2026-10-29 on a worker, for MuLocBench snapshots of small Python repos: 1.3 to 6 s per
graph, plus the first clone. A second commit of the same repo is quicker. Large repos take minutes per graph.
`builds.jsonl` records each build, and `run` estimates from it, so a set's time comes from its own builds:

- `mulocbench`: 46 repos; mostly small and medium apps, a few large (pandas, transformers, cpython, pytorch).
- `locbench`: 165 Python repos, many large (django, sympy, scikit-learn, home-assistant).
- `swe-explore` and `contextbench`: the SWE-bench repos, in 8 languages for ContextBench, with few small ones.

The first full measurement of each set is in the baseline summary under `benches.SET.build`.

## Baselines (step 3)

`bench/results/baseline.json` (shipped ranking, with the evo-agents set), `baseline-bm25.json` (plain BM25) and
`baseline-hybrid.json` (BM25 plus bge-m3) hold the tune and holdout parts of the four code sets: 300 tasks a set sampled
by repo (`--sample 300 --seed 0`), extractor `8418fefbc7d7eafc`, evo-ak 0.52.1. One `kg bench run` pass asked the three
searches on the same graphs; the per-set summaries were joined with `kg bench merge`. `odoo/odoo` is excluded in every
set (`--exclude-repo odoo/odoo`, recorded in each set's `sample.excluded_repos`): its clone filled the worker's disk.
After an out-of-memory kill the run goes on from per-repo answer shards (`ask_repo_kept`, under `kg/<extractor>/shards`),
one repo at a time with `--jobs 1`; the largest resident set of those builds was 4.3 GB (reactivex/rxjava), so no other
repo needed excluding.

| Holdout primary metric | shipped | plain BM25 | BM25 + bge-m3 |
|---|---|---|---|
| ContextBench line F1 (126) | 0.066 | 0.045 | 0.086 |
| SWE-Explore line F1 (230) | 0.118 | 0.069 | 0.138 |
| LocBench Acc@5 (300) | 0.403 | 0.283 | 0.397 |
| MuLocBench Acc@5 (183) | 0.159 | 0.131 | 0.153 |
| evo-agents hit@5 (33) | 0.939 | - | - |

Failures kept in the summaries: ContextBench 26 tasks without a graph (24 of mui/material, whose clone URL moved to
mui/material-ui: `graphs.RENAMED` now maps it, and the finished run keeps the 24 failures; zeromicro/go 1; anuraghazra/github 1).
LocBench has 53 tasks whose answers are not `.py` files, which the Python extractor cannot rank.

### Hybrid vectors came from OpenRouter

The bge-m3 vectors of `baseline-hybrid.json` came from OpenRouter's embeddings API (model `baai/bge-m3`, 1,024
dimensions), not from a local model. The spend ledger lists provider Parasail for 666 M tokens and DeepInfra for 150 M
(OpenRouter routed part of the calls there; the summary's `settings.embeddings.providers` names both). Cost 8.16 USD, under the
50 USD line. A sample of the API vectors against local bge-m3 (fastembed) has cosine >= 0.99997. The backend is opt-in
(`EVO_KG_EMBEDDINGS=openrouter`); the local path stays the default, and vectors are reused across snapshots by content hash.

### Plain BM25 against the published rows

| | ours (nodes) | ours (whole files) | published BM25 |
|---|---|---|---|
| LocBench Acc@5 | 28.3 | 44.0 | 49.3 |
| MuLocBench Acc@5 | 13.1 (183 holdout tasks); 10.7 on the 300-task holdout sample the file run used | 15.0 | 24.5 |
| SWE-Explore line F1 | 0.069 | - | 0.024 |

Whole-file BM25 (`kg bench run --search files`, `bench/filebm25.py`) ranks the files of the task's commit by Okapi BM25
over their whole text, on the same 300 LocBench holdout tasks. It lifts LocBench from 28.3 to 44.0, so ranking
nodes (symbols, sections, files) rather than files is the main cause of the gap, 16 of 21 points; scoring and sample are
sound. The rest (about 5 points) is probably the 53 non-Python answers; I did not measure that split. Counting distinct files
instead of result positions adds 2 points to plain BM25 (`acc_dfile@k`). MuLocBench rises only from 10.7 to 15.0 and stays
9.5 points under 24.5: that gap is not explained, and I cannot say whether it is the issue text, the answers or the sample.
Step 4 should aggregate node scores per file before it ranks. Our SWE-Explore plain row is above the published 0.024; I have not
looked for why (the published figure may use another line budget).

### p95 latency on an idle worker

Searches only, over built graphs, 60 holdout tasks a set, seed 1, no other job on the worker (the baseline p95s, 426, 376 and
303 ms, were measured beside graph builds): LocBench 159 ms, ContextBench 499 ms, MuLocBench 566 ms. ContextBench and
MuLocBench stay above the 300 ms gate of a5 even idle; each task opens a cold graph, so the figure includes loading it.
Step 4 must not assume the gate holds on these two sets.

### The evo-agents set as a gate

`baseline.json` uses 633 questions rebuilt from local checkouts (`kg eval-questions --project evo-agents`): 600 tune, 33
holdout (the hub withholds holdout answers). Tune hit@5 is 0.28 against 81.08 from `kg eval` on the hub because most
answers no longer exist in a checkout of this branch: only 170 of the 600 tune questions name a file that is there, and hit@5 on
those 170 is 0.88. The rest are 365 plan-step answers on files since renamed or removed, 71 transcript questions (6 answerable,
1 hit), and 12 chat questions (none answerable). The 33 holdout plan-step questions score 0.94. As a no-regression gate the
set is sound only for questions with an existing answer; compare runs on the same summary, not against the hub's figure.

### ContextBench by language

The parts are uneven: tune is mostly C++, Rust and JavaScript (line F1 0.022), holdout mostly Python (75 of 126; 0.066). Rows
by language (holdout, shipped ranking): python 0.093 (75), javascript 0.120 (9), go 0.018 (11), java 0.004 (8), typescript
0.002 (15), c 0.000 (8); tune: javascript 0.044 (61), python 0.051 (7), rust 0.003 (25), cpp 0.000 (23), c 0.000 (4). C and
C++ score 0 on both. The per-task `language` field keeps these rows in each summary. For step 4: a tune-time gain on tune is
mostly JavaScript and cannot show a Python gain, and holdout cannot show a C or C++ one; tune per language and report the
language rows next to the primary.
