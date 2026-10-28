# Measuring retrieval: `kg eval`

`evo-agents kg eval` says whether a change to the graph makes finding things better or worse. It reads a set of
questions with their answers, asks `kg_search` and `kg_context` each question as an agent would, greps the same
checkouts for comparison, and prints, per question type and method, how many answers came back and at what cost.
It only reads: the graph through its tools, the checkouts through `git grep`.

```sh
evo-agents kg eval questions.jsonl                         # the project of the current directory, local store
evo-agents kg eval questions.jsonl --project demo --json > results.json
evo-agents kg eval questions.jsonl --backend hub           # the graph the hub serves
evo-agents kg eval questions.jsonl --grep-ref origin/main  # grep the commit the graph was built from
evo-agents kg eval questions.jsonl --type identifier --method kg_search
evo-agents kg eval questions.jsonl --split tune            # only the part ranking is tuned on
```

The command ships without questions. A team keeps its set, with its plans' text and its paths, on the hub
(`hub kg eval-set`, below), where every machine and the hub's own evaluation read the same version.

## The question set

One JSON object a line:

```json
{"id": "q1", "question": "where are search results ranked", "type": "words", "project": "demo",
 "repo": "app", "files": ["app:app/search.py"], "nodes": ["symbol:app:app/search.py::rank_results"]}
```

| Key | Meaning |
| --- | --- |
| `question` | what the tools are asked, and what grep looks for (required) |
| `type` | the group the question is reported in, such as `plan-step`, `transcript` or `identifier` (required) |
| `files` | answer files: `repo:path`, or a path in any repo |
| `nodes` | answer nodes: node ids or aliases |
| `project` | the graph to ask (default: `--project`, else the project of the current directory) |
| `repo` | grep reads only this repo of the project |
| `id` | the question's name in the results, and what fixes its part (default: `line N`) |

A question needs at least one answer, and each entry of `files` and `nodes` is one answer. Other keys are left for
whoever wrote the set. A bad line stops the command before anything runs; the error names the bad lines, the first 20 of them.

The first line may be a header instead of a question, `{"set": "evo-agents", "version": 3}`: the set's name and
version, which the report names below the table and in `--json` (`settings.set`, `settings.version`).

### Tune and holdout

Each question falls for good in one part of the set by the SHA-256 of its id: 30% of the ids in `holdout`, the rest
in `tune`, the same on every machine and on the hub. Renaming an id moves its question; editing its text or answers
does not. `--split tune` or `--split holdout` asks one part (default `all`). Tune ranking on `tune` only: the hub
keeps the holdout answers to itself, and the ranking code may not import the evaluation (lint-imports contract
`kg-ranking-not-eval`), so a change that helps only the questions it was tuned on shows up as a gap between the parts.

### The set on the hub

```sh
evo-agents hub kg eval-set push questions.jsonl --project demo     # the next version (writer role)
evo-agents hub kg eval-set pull --project demo --split tune -o tune.jsonl
evo-agents hub kg eval-set pull --project demo --split holdout     # the holdout questions, without answers
```

`push` puts the file as the project's next version, or reports the latest one again when its questions ask and
answer the same; every question needs an `id`, and the header's `set`, or `--set`, names it. `pull` writes the latest
version (`--version N` another) as a set file, header first. A holdout question never carries its `files` and
`nodes` off the hub; `--no-answers` strips the tune answers too. The API is `PUT` and `GET /v1/kg/{project}/eval-set`.

## Methods

Every method gets the same fixed budget on every question:

| Method | Call | Ranked list |
| --- | --- | --- |
| `kg_search` | `query` = the question, `limit` = `--limit` (10) | its results, in order |
| `kg_context` | `query` = the question, `budget_tokens` = `--budget-tokens` (3000) | its nodes, seeds first |
| `grep` | `git grep -i -F` of the question's terms in each checkout | the files its output shows |

Grep's terms are the whole question when it has no space, then its words of three letters or more that are not stop
words, at most 8. Files rank by how many terms they hold, then by how many lines match, then by path. The output is
`path:line:text`, at most 3 lines a file, cut at `--budget-tokens`; a file counts only when one of its lines is shown.
Grep reads the checkouts of the project's repos that are git working trees on this machine, and the harness when it
is one; `--repo NAME=PATH` adds or replaces one. A question whose repo has no checkout is skipped by grep and left out
of grep's numbers. With `--grep-ref`, grep reads that commit's tree in every checkout instead of the working tree.

A node answers a file when its path (`props.path`, or the file a section is part of) is that file and its repo, when
the node names one, is the answer's. It answers a node when its id or one of its aliases is that node. A file grep
shows answers a file the same way, and a node whose id names a file (`symbol:R:P::Q`, `R:file:P`): grep finds a
symbol by the file that defines it.

## The numbers

| Column | Meaning |
| --- | --- |
| `N` | questions the method ran on |
| `R@1`, `R@5` | share of a question's answers among its first 1 or 5 results, averaged over the questions |
| `HIT@5` | share of questions with at least one answer among the first 5 results |
| `R@5 CAP` | answers among the first 5 over min(5, the question's answers), averaged: a question of 40 files can score 1 |
| `MRR` | 1 / rank of the first answer found (0 when none is), averaged |
| `EMPTY` | share of questions with no result; an error counts as none |
| `OK/1K TOK` | answers found anywhere in the output, per 1,000 tokens of output, over all the questions |
| `MEAN TOK` | tokens of one output, on average |
| `TOK/HIT` | `MEAN TOK` / `HIT@5`: tokens read per question answered (empty when nothing was) |
| `TOK/FILE` | `MEAN TOK` / `R@5`: tokens read per answer file found (empty when none was) |
| `P95 MS` | 95th percentile of the time one call took (nearest rank) |

Tokens are the output's UTF-8 bytes / 3, rounded up, as the hub's handoff counts them. The text the model would read
is counted: the text block of a kg tool's result, grep's lines. A row `all` per method closes the table. Below it, a
line per method says how many calls answered with an error (`kg_context` answers "nothing visible matches" as one)
and how many questions were skipped. `--json` prints the settings, the rows, and per question and method the rank of
each answer (`files` first, then `nodes`; `null` when not shown), the count of results, the tokens and the time.

The command exits 1 when a method answered with an error on every question it ran, which points at the set or the
backend rather than the ranking, and 2 on a bad set or option.

## Questions from the hub's plans

```sh
evo-agents kg eval-questions --project demo > plan-steps.jsonl
evo-agents kg eval-questions --plan search --text what --repo app=../demo-app
```

Each step that is done and whose evidence names commits becomes a question of type `plan-step`: its title (or its
`what` with `--text what`) asks, and the answers are the files those commits changed in a checkout of the step's repo
(its own `repo`, else the plan's only repo), as `repo:path`. A merge commit counts what it changed against its first
parent. A step is left out, with the reason on stderr, when it has no checkout here, when none of its commits is in
the checkout, or when its commits change more than `--max-files` (20) files. The command reads the hub's plans and
the checkouts' history, nothing else.

## The hub's evaluation

Every 6 hours the hub measures its latest graph of each project (`kg_health`, the `eval` key of a measure). It asks
`kg_search` the holdout part of the project's latest set, then a `plan-step` question for each done step of the hub's
plans whose id the set does not hold, through the agent sink `claude-code@anthropic` when the project's policy
declares it (else the hub sink), as the charter's owner reads.

The hub has no checkout, so a step's answers are the files of the graph its commits changed. When those commits were
squashed away, the answers come from the pull request that merged the step's branch or that its evidence cites, then
from the paths its evidence names. A step is left out only when it names no repo, when none of that finds a file, or
when it has no title; `eval.skipped_by` counts the reasons and `eval.dropped` the share left out. A step whose work
changed more than 20 files stays in. `eval.by_type` keeps the numbers per type.

After each measure with a score the hub pushes two signals as the owner: `kg.retrieval.hit_at_5` (percent) and
`kg.retrieval.tokens_per_file`, both of `kg_search` over all the questions.
