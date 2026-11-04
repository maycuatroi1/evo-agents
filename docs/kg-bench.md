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
kg/EXTRACTOR/home/PROJECT/bench.json the marker of a finished build: its size; its mtime is the graph's last use
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

**Room and time.** The graphs are a cache that grows by up to 700 MB a snapshot; the four sets once made 400 GB of
them and filled a worker's disk. Before each build, `build` and `run` make room:

- They evict graphs while the graphs pass `--cache-gb` (default 200; 0: no cap) or the disk would have less than
  `--reserve-gb` free after the build (default 50, or a tenth of a smaller disk).
- Another extractor's graphs go first, since no run reads them any more; an extractor left with none goes whole, with
  its memo and shards. Then this extractor's graphs go, the least recently used first.
- A graph the run still needs for its repo's later tasks stays, and so does any graph a run used within the last hour,
  since another process may be reading it. The cap can be passed by that hour's graphs; the reserve cannot.
- When evicting cannot keep the reserve, the run stops with an error before it writes anything: no summary, and the
  repos it finished stay in the cache's shards, so the same command goes on from them once there is room.

Clones (`repos/`) and memos are outside the cap; the reserve covers them. `--prune` also deletes each snapshot's graph
after its last task, and each repo's clone and memo after its last snapshot, so a run needs room for the graphs in
flight only. `--jobs N` builds and asks N repos at once, the largest first, each in its own process.

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
- A summary of one part names it as its `split` (the edge model's `kg links eval` writes `"split": "holdout"`), and
  the checks read that part when `--split` is not given ([docs/kg-links.md](kg-links.md)).

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

Whole-file BM25 (`kg bench run --search files`, `evo_agents/kg/filebm25.py`) ranks the files of the task's commit by Okapi BM25
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
those 170 is 0.88. The other 430 are 275 plan-step and 78 identifier questions whose files were renamed or removed or never lived in
this branch, 65 of 71 transcript questions and all 12 chat questions (these name no file); I did not check each one. The 33 holdout plan-step questions score 0.94. As a no-regression gate the
set is sound only for questions with an existing answer; compare runs on the same summary, not against the hub's figure.

### ContextBench by language

The parts are uneven: tune is mostly C++, Rust and JavaScript (line F1 0.022), holdout mostly Python (75 of 126; 0.066). Rows
by language (holdout, shipped ranking): python 0.093 (75), javascript 0.120 (9), go 0.018 (11), java 0.004 (8), typescript
0.002 (15), c 0.000 (8); tune: javascript 0.044 (61), python 0.051 (7), rust 0.003 (25), cpp 0.000 (23), c 0.000 (4). C and
C++ score 0 on both. The per-task `language` field keeps these rows in each summary. For step 4: a tune-time gain on tune is
mostly JavaScript and cannot show a Python gain, and holdout cannot show a C or C++ one; tune per language and report the
language rows next to the primary.

## Without training (step 4)

`bench/results/no-train.json` holds the holdout part of the four code sets and both parts of the evo-agents set, asked
with `kg_search` as it ships after step 4: the same tasks as `baseline.json` (`run --like bench/results/baseline.json
--part holdout`), the same graphs (extractor `8418fefbc7d7eafc`), each set run apart with `--jobs 2` and joined with
`kg bench merge`. The runner now reads each graph once before it asks (`graphs.warm`): the baseline's p95 timed a cold
disk read of graphs built hours before (django: 829 ms on the first search, 160 ms on the next), which a served graph
does not pay.

What changed in the ranking (docs/kg.md, `kg_search`): a caller that names a `limit` gets that many results even when
none holds the whole query; on a code graph the text matches are read again by `evo_agents/kg/locate.py` (exact
anchors from the issue, expansion along parsed or confident edges, tests and co-changed files, scores per file, one
symbol per file before any file's second).

| Holdout primary metric | baseline | no-train | difference (95% interval) |
|---|---|---|---|
| ContextBench line F1 (126) | 0.066 | 0.082 | +0.016 (+0.003, +0.031) |
| SWE-Explore line F1 (230) | 0.118 | 0.160 | +0.042 (+0.019, +0.065) |
| LocBench Acc@5 (300) | 0.403 | 0.490 | +0.087 (+0.050, +0.127) |
| MuLocBench Acc@5 (183) | 0.159 | 0.186 | +0.027 (-0.016, +0.071), within the interval |
| evo-agents hit@5 (33 paired) | 0.939 | 0.939 | 0 |

The evo-agents set was asked from 437 questions made by `kg eval-questions --project evo-agents` on this branch (303 tune,
134 holdout), against 633 in the baseline; `compare` pairs them by id, so its row holds the 33 holdout questions both ask.
kg_search p95 over the holdout of all five sets: 188 ms (each set alone 169 to 244 ms). ContextBench by language (holdout): python 0.093 to 0.116
(75), javascript 0.120 to 0.156 (9), go, java, typescript and c unchanged near 0. SWE-Explore: python 0.132 to 0.183 (194).

**Tuning, on the tune part only.** The weights of `locate.Params` came from a random search over pools of candidates
gathered once per tune task of SWE-Explore (70), ContextBench (172) and MuLocBench (117), scored offline with the
bench's own metrics; LocBench has no tune part. No holdout task was read while tuning. Each direction, on those pools
(line F1, line F1, Acc@5):

| On the tune pools | SWE-Explore | ContextBench | MuLocBench |
|---|---|---|---|
| text order of the matches | 0.043 | 0.042 | 0.197 |
| locate as shipped | 0.119 | 0.067 | 0.291 |
| without anchors | 0.078 | 0.059 | 0.248 |
| without expansion along edges | 0.114 | 0.045 | 0.282 |
| one spot per file first off (`second=0`) | 0.137 | 0.062 | 0.128 |
| per-token packing (`size_pen` 0.5) | 0.115 | 0.065 | 0.291 |
| cut files under 0.2 of the best (`weak`) | 0.122 | 0.063 | 0.231 |

Through the real `kg_search` path on the tune graphs still built (SWE-Explore 70, ContextBench 172; the MuLocBench tune
graphs had been evicted): text order with the cut lifted 0.057 and 0.052, locate with the `WEAK_LIMIT` cut kept 0.106 and
0.049, locate without it 0.106 and 0.070. A coordinate search that raised the pool objective from 0.825 to 0.891
(anchor 3, expand 0.3, file_extra 0, test_path 0.3) moved the real path to 0.111 and 0.063, so the shipped weights stay.

**ContextBench answers under `/workspace/`.** 354 of the 811 ContextBench tasks give their answer paths as
`/workspace/<owner>__<repo>__<version>/<path>`, which no graph path matches, so those tasks score 0 with any ranking
(none of Python's 199 tasks; 52 of 67 C, 52 of 57 C++, 49 of 57 Java, 47 of 61 Rust, 52 of 101 Go tasks: most of the
near-zero rows by language). The reader in `datasets.py` should strip that prefix; doing so changes the
answers, so it needs the baselines asked again and is left to a later step.

## Learned ranking (M1, step 5)

`evo_agents/kg/rank.py` holds a LightGBM LambdaMART model that picks the first results of a code question. locate
orders files and places their nodes as before; the model then scores the first 6 nodes of each of locate's first 15
files and puts the 3 it scores best first. locate's own order follows, without those 3. Each node is described by
numbers only, so the model file holds no text of any project:

- **Its file (55 numbers):** BM25 of the file's best match and its share of the best file's score, locate's place and
  gap, how much of the query the file's name, path and symbols cover, exact matches, anchors and hops from them,
  expansion per edge kind (calls, imports, inherits, overrides, changes, mentions, tests, co-changed), parsed and
  resolved links, PageRank over the pool's edges seeded with the text scores, file kind (test, source or doc), path
  depth and lines, same directory as an anchor, a linked test, imports in, symbols defined, commits and the age of the
  last one, the asked weight, and the cosine of the query's vector (missing when the graph has none).
- **The node itself (23 numbers):** where locate placed it, its score next to its file's best, its text match and
  anchor, its kind and the lines it spans, whether the question names it, and the number of results asked for.

The model ships in the package as `evo_agents/kg/rank_model.txt` (LightGBM's text format: feature names, thresholds and
leaf values, 300 trees, 542 KB). kg_search asks it only when the `rank` extra is installed (`pip install
'evo-ak[rank]'`: LightGBM and NumPy; `hub-server` pulls it in) and the setting is on. The setting is a session's `rank`,
else `EVO_KG_RANK=on|off`, else `rank.DEFAULT_ON`. Without LightGBM or the model file, or with the setting off, the
order is locate's, unchanged. **It ships off**, though the gate below passed: on by default, it changes orders that
existing tests of `serve`, `eval` and `hybrid` fix on their small graphs (a plan step before the file it mentions, a
section found by meaning before code), which invariant a13 keeps. Turning it on is a decision of its own, together
with those tests.

With the model on, a pool whose best text matches are mostly documents is ordered the same way; without it, such a
pool keeps the text order. On the evo-agents tune questions this moved hit@5 from 0.660 to 0.670 and MRR from 0.347 to
0.631 (their plan steps are among the training labels, so this is no holdout number). Handing documents back to the
text order instead gave 0.673 and 0.428. `kg bench run` puts the setting and the model's digest in the summary's
`settings.rank` and in the key of its cached answers, so a run with the model never reuses the answers of one without
it.

**Training: `evo-agents kg rank train`.** None of the labels come from a holdout or test repo (`rank_train.tune_only`).
Each label is asked the way kg_search asks it with the model on. Every node the model would score becomes a row with a
grade: 0 outside the answer, 1 in an answering file but off its lines, 2 to 5 as more of its lines (10, 30, 60%) are
answer lines.

| Source | groups | weight |
|---|---|---|
| bench tune tasks (SWE-Explore, MuLocBench and ContextBench, 150 each sampled, seed 0), files and lines | 458 | 1.0 |
| commits after each tune graph's commit (40 per graph, the nearest first), the files and the lines their hunks replaced | 8,018 | 1.0 |
| evo-agents plan steps, tune split of `kg eval-questions` (files only) | 306 | 0.5 |
| hub clicks weighed by inverse propensity (`--clicks`) | 0 | |

A commit is a label unless a bot made it, its message (PR refs and trailers removed) has fewer than 4 words, it
changes more than 8 files once lock files, generated files and change logs are left out, or one of its files changed
between the graph's commit and the commit's parent (its line numbers would not be the graph's). Those thresholds are
step 5's first attempt's, picked by cross-validation of file MRR. ContextBench answers under `/workspace/<repo>/` are
read as the repo's own paths for training only; the bench's reader is unchanged, so the holdout numbers below stay
comparable with `no-train.json`. The hub keeps click labels only in its database, which no route reads; the shipped
model has none, and `--clicks FILE` takes them as JSON lines (query, results, clicks) once an export exists.

The commit weight, the number of trees and the number of places the model fills were chosen by 5-fold
cross-validation over tune repos, scoring each set's primary metric on the bench labels of the held-out repos:

| tune, cross-validated | ContextBench line F1 (152) | MuLocBench Acc@5 (161) | SWE-Explore line F1 (145) |
|---|---|---|---|
| locate's order | 0.046 | 0.230 | 0.092 |
| commits at 0.25, 300 trees, 3 places | 0.048 | 0.261 | 0.136 |
| commits at 0.5, 300 trees, 3 places | 0.050 | 0.261 | 0.132 |
| commits at 1.0, 300 trees, 1 place | 0.049 | 0.273 | 0.127 |
| commits at 1.0, 300 trees, 3 places (kept) | 0.048 | 0.273 | 0.138 |
| commits at 1.0, 100 trees, 3 places | 0.049 | 0.267 | 0.133 |

900 trees did no better than 300. The first attempt of this step reordered locate's first 30 files and kept locate's
node placement. It raised file MRR on every holdout set (ContextBench 0.403 to 0.454, LocBench 0.543 to 0.608) but no
primary metric beyond its 95% interval, and SWE-Explore's line F1 fell by 0.009: a file placed higher brought its
larger nodes into the 5 results. Grading nodes by line overlap, and letting the model fill only the first places,
fixed that.

**Feature importance** (share of the total gain, 300 trees): place of the first text match 0.214, node spans its whole
file 0.088, expansion along change edges 0.074, locate's gap 0.056 and ratio 0.046, PageRank 0.041, age of the last
commit 0.037, the file's and the best node's score next to the best 0.036 and 0.030, node lines 0.029, commits 0.026,
locate's score 0.025, path cover 0.023. Anchors weigh 0.001 to 0.003, since locate's own score already holds them.
**Cosine 0**: no bench graph has vectors (`--search graph`), so the feature is missing in every row and no tree splits
on it. The same holds for the asked weight, as no bench graph has clicks. So these numbers cannot say whether vectors
add anything beyond the rest; that needs a model trained on graphs built with `semantic`, which is the input M5's
decision needs.

**Holdout, `bench/results/m1.json` against `no-train.json`** (the same tasks, `--like no-train.json --part holdout`,
`EVO_KG_RANK=on`, one set at a time, merged with `kg bench merge`):

| Holdout primary metric | no-train | M1 | difference (95% interval) |
|---|---|---|---|
| ContextBench line F1 (126) | 0.082 | 0.101 | +0.019 (+0.002, +0.037) |
| SWE-Explore line F1 (230) | 0.160 | 0.211 | +0.051 (+0.033, +0.071) |
| LocBench Acc@5 (300) | 0.490 | 0.530 | +0.040 (+0.010, +0.070) |
| MuLocBench Acc@5 (183) | 0.186 | 0.230 | +0.044 (+0.011, +0.082) |
| evo-agents hit@5 (134, not a target) | 0.627 | 0.605 | -0.022 (-0.067, +0.022), one standard error down |

File MRR rose too: ContextBench 0.406 to 0.519, SWE-Explore 0.679 to 0.706, LocBench 0.543 to 0.610 (Acc@1 0.317 to
0.400), MuLocBench 0.357 to 0.386 (Acc@1 0.071 to 0.115). SWE-Explore's line precision rose from 0.385 to 0.472 where
the first attempt lowered it. kg_search p95 with the model: 200.6 ms over the five sets (ContextBench 242, SWE-Explore
194, LocBench 193, MuLocBench 201, evo-agents 114), against 169 to 244 without it. SWE-Explore was asked again alone
for its times: its first run shared the machine with a MuLocBench run and timed 410 ms at p95, and 33 of its tasks
were skipped (their builds collided with that run's: database locked); the rerun built them and asked all 230.

**Gate (a6): passed.** All four sets improve beyond their 95% interval (three needed), none falls by more than two
standard errors, and p95 is under 300 ms. The model still ships off (see above): with `rank.DEFAULT_ON = True`,
5 of the 113 tests of rank, store, serve, eval and hybrid fail. Applying the model only to pools that read as code, as
locate does, still leaves 3 of them failing.

## Memory sets: LongMemEval and MemoryAgentBench (step 8)

Two more sets score how well `kg_search` finds the chats a question needs. Each case is a haystack of chat sessions,
built into a graph of its own the way the hub puts its chats into the graph (`evo_agents/hub/server/chat_graph.py`):

- A session becomes a Chat item.
- The Chat has one ChatThread item, rooted at its first turn.
- Each turn becomes a ChatMessage item. The user is a person named `user`, the assistant an agent named `@assistant`,
  and the message is sent at the session's date.

The items reach the build through an in-process connector, `evo_agents.kg.bench.memory:connector`. The code is in
`evo_agents/kg/bench/memory.py`.

```sh
evo-agents kg bench fetch longmemeval memoryagentbench
evo-agents kg bench run longmemeval memoryagentbench --jobs 4 --out bench/results/memory-baseline.json
evo-agents kg bench show bench/results/memory-baseline.json --require longmemeval,memoryagentbench --split holdout
```

| Set | Source (Hugging Face, pinned, MIT) | Cases | Scored |
|---|---|---|---|
| `longmemeval` | `xiaowu0162/longmemeval-cleaned`, `longmemeval_s_cleaned.json` (the cleaned S variant) | 500 questions, about 48 sessions each | 470, leaving out the 30 abstention questions |
| `memoryagentbench` | `ai-hyz/MemoryAgentBench`, `data/Accurate_Retrieval-*.parquet` and `data/Conflict_Resolution-*.parquet` (selective forgetting) | 30 contexts, 2800 questions | the 300 questions of its 5 `longmemeval_s*` contexts, less abstention |

**Scoring.** The scoring follows LongMemEval's `print_retrieval_metrics.py`:

- A question whose id holds `_abs` is left out.
- `kg_search` gets the question verbatim, with its largest limit (25).
- Each result counts for the session its message, thread or chat is in. Sessions count in the order they first
  appear.
- `recall_all@k` is 1 when every answer session is among the first k sessions, and `recall_any@k` when one is.
  `ndcg_any@k` follows the set's `eval_utils`.
- The primary metric is `recall_all@5`.

The answer sessions are LongMemEval's `answer_session_ids`. In MemoryAgentBench they are the sessions its
`haystack_sessions` names for each question, matched to the context's sessions by their turns. One of the 300 matches
no session, so its question can never reach `recall_all`.

The other MemoryAgentBench datasets give no evidence to score retrieval by: EventQA, RULER QA and FactConsolidation.
Their graphs are built, and the summary marks each of them `no retrieval labels`. Their context, a novel, documents
or a list of facts, becomes one chat of chunks of at most 2048 characters each. The summary also gives every metric
per LongMemEval question type and per ability, and for MemoryAgentBench per dataset.

**Parts.** A question's part comes from `repo_split("longmemeval/<question id>")`. MemoryAgentBench's `longmemeval_s*`
questions are LongMemEval questions, so each falls in the same part in both sets. A case without labels is built
whatever its part, since nothing in it is scored.

**Baseline** (`bench/results/memory-baseline.json`). This baseline uses `kg_search` as it ships, the ranker off, and
extractor `8418fefbc7d7eafc`. Builds took 953 s for the 365 LongMemEval graphs and 96 s for MemoryAgentBench's 30
(tune and holdout, `--jobs 4`). The memory sets were not optimised.

| `recall_all@5` (`@10`) | tune | holdout |
|---|---|---|
| LongMemEval S, all | 0.833 (0.900), 251 | 0.816 (0.886), 114 |
| information extraction | 0.938 (0.975), 81 | 0.946 (0.973), 37 |
| multi-session reasoning | 0.590 (0.721), 61 | 0.679 (0.821), 28 |
| knowledge update | 1.000 (1.000), 47 | 1.000 (1.000), 15 |
| temporal reasoning | 0.806 (0.903), 62 | 0.706 (0.794), 34 |
| MemoryAgentBench `longmemeval_s*` (about 111 sessions a context) | 0.715 (0.808), 151 | 0.620 (0.732), 71 |

## Non-code sets: seven sets of text (step 11)

`kg bench` also scores seven sets that are not code. Together they are the group `noncode`, which every command takes
as a name (`evo-agents kg bench fetch noncode`). `longmemeval` and `memoryagentbench` are the memory sets above, with
a graph for each haystack. The other five are *corpus* sets: all documents of a set go into one graph, and every
question searches it, the way a user's questions search a project's graph. The code is in
`evo_agents/kg/bench/corpus.py`.

```sh
evo-agents kg bench fetch noncode
evo-agents kg bench run noncode --jobs 4 --out bench/results/noncode-base.json
evo-agents kg bench show bench/results/noncode-base.json --split holdout \
  --require musique,longmemeval,memoryagentbench,medical,literature,music,customers
```

| Set | Source (pinned in `datasets.CORPUS_SOURCES`) | License | Documents | Questions kept | Answer |
|---|---|---|---|---|---|
| `musique` | `dgslibisey/MuSiQue` at `c8f4f8c9465f`, `musique_ans_v1.0_dev.jsonl` | CC-BY-4.0 (StonyBrookNLP/musique; the Hugging Face copy names none) | 7921 paragraphs | 600 of 2417 | the supporting paragraphs (2 to 4) |
| `longmemeval` | `xiaowu0162/longmemeval-cleaned` | MIT | a graph a question | 365 | the answer sessions |
| `memoryagentbench` | `ai-hyz/MemoryAgentBench` | MIT | a graph a context | 222 scored | the answer sessions |
| `medical` | `qiaojin/PubMedQA` at `9001f2853fb8`, `pqa_labeled` | MIT | 1000 abstracts | 1000 | the abstract the question was asked about |
| `literature` | `deepmind/narrativeqa` at `2e643e736394`, `data/narrativeqa-master.zip` | Apache-2.0 (the summaries are Wikipedia text, CC-BY-SA) | 1572 plot summaries | 1000 of 46765 | the story the question was written about |
| `music` | `seungheondoh/music-wiki` at `cdf5bfb7850c`, MusicBrainz genre, instrument and artist files | MIT (the text is Wikipedia, CC-BY-SA; the metadata MusicBrainz, CC0) | 3829 pages | 800 | the page the question describes |
| `customers` | a seeded generator, nothing downloaded | none needed | 1500 records | 800 | the records the question fits (at most 3) |

A set with no license that allows the use is not used. The cache is outside any repo here too (`EVO_KG_BENCH_CACHE`),
and a summary holds ids and scores, never text.

- **Questions and answers.** MuSiQue's question is read as it is, and only a question with an answer (`answerable`) is
  kept, since every question of `kg eval` needs one. PubMedQA's and NarrativeQA's are read as they are. For `music`
  the question is the first sentence of a page with its name taken out ("Which genre page says: this is a ...?"),
  because the set has no questions. The answers are documents, the files of the corpus: a result counts for the
  document its id names, and a section of a document counts for the document.
- **Cap.** At most 600 (`musique`), 1000, 1000, 800 and 800 questions are kept, the ones with the smallest SHA-256 of
  the seed and the id, so a graph stays within a few thousand documents. For `musique` only the paragraphs of the kept
  questions are read. For `music` the artists are the 1500 with the smallest hash of 7002.
- **Customers.** `generate_customers` makes 1500 records from seed 20261104. Names are built from syllables
  (such as "Nehoux Dapir") and so are companies and cities: no real person, company or town, no contact detail.
  A record holds a plan, seats, an account note and one to three support tickets. A question gives some of a record's
  facts in one of three styles (plan, city and a ticket; plan, city and the note; company and seats). Its answers
  are every record those facts fit, and a question that would fit more than 3 gets the seat count added.
- **Graph.** Each document is one item `corpus:doc:N`, kind `document`, its text the body and its metadata (MeSH terms,
  entity type, MusicBrainz id, plan and city) the props, labelled public. The items reach the build through the
  memory sets' in-process connector. The graph is named by a digest of the documents, so a change of the corpus builds
  it again; the same corpus is reused.
- **Scoring** is the memory sets': `recall_all@k` is 1 when every answer document is in the first k documents,
  `recall_any@k` when one is, `ndcg_any@k`; `kg_search` gets the question verbatim with limit 25. The primary metric is
  `recall_all@5`. The summary gives it per question kind (hop count, yes/no/maybe, book or movie, genre,
  instrument or artist, customer style).
- **Parts.** A question falls in `tune`, `holdout` or `test` by the SHA-256 of `<set>/<question id>`, with the shares of
  the code sets (a half, a quarter, a quarter). `test` is sealed as before. `--sample N` samples the questions
  asked, not the corpus.
- **Search.** The corpus and memory sets take `--search graph` only, and `--limit` 1 to 25.

The code sets are untouched: `kg bench` without a set name still runs the four.

Baseline, `bench/results/noncode-base.json` (release ranking, `recall_all@5`, `--jobs 4`):

| Set | tune | holdout |
|---|---|---|
| `musique` | 0.1163 (n 301) | 0.1266 (n 158) |
| `longmemeval` | 0.8327 (n 251) | 0.8158 (n 114) |
| `memoryagentbench` | 0.7152 (n 151) | 0.6197 (n 71) |
| `medical` | 0.9881 (n 503) | 1.0000 (n 237) |
| `literature` | 0.9024 (n 502) | 0.9434 (n 265) |
| `music` | 0.9930 (n 428) | 0.9838 (n 185) |
| `customers` | 0.9847 (n 392) | 0.9598 (n 199) |

`musique` is low because a question needs all 2 to 4 supporting paragraphs in the first 5 results. `medical` is
near its ceiling. The `music` questions are made from the page text, not written by people, so read them as a
retrieval check and not as a quality measure of answers.

## The Curator on the benches (step 12)

Does the Curator's kg_init choice, made from a set's data profile and its tune numbers alone, rank as well as the best
fixed config on the holdout? `bench/results/curator.json` answers that for all 11 sets.

**The run.** For each set `kg bench curator` does four things:

1. `plan SET` writes the configs to rank: `default` (the release defaults), `strategy-conversation`,
   `strategy-media-metadata`, `strategy-records` (the query keys each built-in strategy sets), `weights-body-heavy`
   (`bm25_body` 4), `weights-name-light` (`bm25_name` 2, `bm25_path` 1) and, on the code sets, `plain-bm25`.
2. Each config runs on the tune part with `kg bench run --settings`.
3. `decide SET` builds one prompt from the skill text of the lens (agent-skills, `skills/curator`, `kg_init`), the
   set's merged data profile (counts and shapes, no content), the strategy list and the tune table (score, n, SE per
   config), and asks the agent runtime. The answer is the kg_init decision JSON of `init_run.parse_decision`. The
   inputs are written to `curator-inputs/SET/` with a SHA-256 each (`prompt.md`, `profile.json`, `strategies.json`,
   `tune-scores.json`, the two skill files, `decision.json`). No question file or answer of holdout or test is among
   them. A set without a tune part (LocBench) gets the profile and the line "no tune questions".
4. The decision becomes a config (`kg bench` applies its query keys; `unapplied` lists build keys and `semantic_mode`,
   which a bench run cannot change), runs as the extra candidate `curator` on the holdout with all the others, and
   `assemble SET... --out FILE` writes the file. `--defaults` writes the same runs ranked with no Curator decision.

**File format.** `curator.json` is an `evo-kg-bench/1` summary (the chosen config's tasks, so `compare` and `show`
read it as any other) plus a top-level `curator` block: `format` (`evo-kg-bench-curator/1`), `split`, `note`,
`sampling`, and per set: `chosen` (candidate, `strategy` with `name`, `self_made`, `origin`, `steps`, and `settings`,
`applied`, `unapplied`), `reasons` (the Curator's, with figures), `inputs` (name, path, SHA-256, part), `profile_graphs`
and `candidates`. A candidate has `name`, `kind`, `search`, `settings` and, per part, `n`, `primary`, `mean_tokens` and
`tasks` (id, value, tokens), so any two candidates can be paired per question.

**The check.** `kg bench show FILE --require SETS --split holdout --curator-agrees N` exits nonzero when:

- fewer than N of the required sets have the Curator's config within two standard errors of the best candidate (the
  paired bootstrap of `compare`; the best is the candidate with the highest holdout mean),
- a set has fewer than 4 candidates besides the Curator's, or no input list,
- an input list holds a file of the holdout or test part (by its part, its name or its path), has a malformed SHA-256,
  or a file next to the summary whose SHA-256 differs.

`kg bench compare bench/results/curator-base.json bench/results/curator.json --split holdout --gate` then tests the
Curator against the release defaults (`curator-base.json`, the ranking of origin/main).

**What the Curator did.** It ran as a Claude subagent (model sonnet) that read only the one prompt file. A nested
`claude -p` had no login on the machine, so the usual `EVO_KG_INIT_AGENT` runtime was replaced by a relay that handed
each prompt over and took the answer back; the cost is not metered (about 20 000 tokens a set, 11 calls, one per set).

| Set | Strategy | Self-made | Query keys applied | Curator holdout | Best candidate | gap / SE |
|---|---|---|---|---|---|---|
| `contextbench` | `code` | no | none | 0.0937 | 0.0937 (curator) | 0 |
| `swe-explore` | `code` | no | none | 0.1401 | 0.1401 (curator) | 0 |
| `locbench` | `code` | no | none | 0.5405 | 0.5405 (curator) | 0 |
| `mulocbench` | `code` | no | none | 0.3158 | 0.3684 (weights-body-heavy) | 0.0526 / 0.0525 |
| `musique` | `code` | no | none | 0.1266 | 0.1392 (weights-name-light) | 0.0127 / 0.0157 |
| `longmemeval` | `conversation` | no | `semantic_weight` 0.8 | 0.8333 | 0.8333 (curator) | 0 |
| `memoryagentbench` | `conversation` | no | `semantic_weight` 0.8 | 0.6197 | 0.6197 (curator) | 0 |
| `medical` | none | no | none | 1.0000 | 1.0000 (curator) | 0 |
| `literature` | `code` | no | none | 0.9434 | 0.9472 (weights-name-light) | 0.0038 / 0.0038 |
| `music` | none | no | none | 0.9838 | 0.9838 (curator) | 0 |
| `customers` | `code` | no | none | 0.9598 | 1.0000 (weights-body-heavy) | 0.0402 / 0.0139 |

Reading it honestly:

- The Curator made no strategy of its own and moved no query key that mattered. On tune, the candidates tied or sat
  inside noise, and it kept the defaults, as the skill tells it to when no figure beats the default by two standard
  errors. Its config equals the release default in effect on all 11 sets (on `longmemeval` and `memoryagentbench` the
  `semantic_weight` of 0.8 changes nothing, as the graphs hold no vectors). So the gate against `curator-base.json`
  shows a difference of 0 everywhere, and "within two SE of the best" is easy where all candidates tie. The result
  says the Curator does no harm and picks safe settings, not that it finds gains.
- Where a candidate beat the default on the holdout (`customers`, `mulocbench`, `musique`, `literature`), the tune
  numbers had not shown it: on `customers` the tune gap of `weights-body-heavy` was 0.0102 against 2 SE of 0.0124, and
  the holdout gap is 0.0402 (2.9 SE). The Curator stayed inside the rule, and the rule lost there. That is the one set
  of 11 that misses.
- Targets: code sets 4 of 4 (needed 3), non-code sets 6 of 7 (needed 5), the gate passes.

**Limits.**

- Sampling, not full runs. Code sets: repos over 100 MB left out, then 40 tasks per part (`--sample 40 --seed 0`,
  stratified by repo), so the holdout n is 37 to 40 and the SE is wide; a gap under 0.05 cannot be told from noise.
  `longmemeval` and `memoryagentbench` use `--sample 60 --seed 0`. The other five non-code sets run in full.
- Only the query keys are measured. Build keys (`passage_from`, `passage_chars`, `embed_chars`) and `semantic_mode`
  change the graph and are not applied in a bench run, so a strategy's build side is never tested here.
- No hybrid candidate: `fastembed` is not installed on the machine, so no vectors exist and `semantic_weight` has
  nothing to weigh.
- LocBench has no tune part. The profile of its sampled graphs is all the Curator had there.
- On a set without a distinct class (`medical`, `music`: all items of class `other`) the Curator named no strategy.
