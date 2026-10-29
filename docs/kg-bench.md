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
