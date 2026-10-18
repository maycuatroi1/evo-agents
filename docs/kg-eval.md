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
```

The command ships without questions. A team keeps its set, with its plans' text and its paths, in its harness.

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
| `id` | the question's name in the results (default: `line N`) |

A question needs at least one answer, and each entry of `files` and `nodes` is one answer. Other keys are left for
whoever wrote the set. A bad line stops the command before anything runs; the error names the bad lines, the first 20 of them.

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
| `MRR` | 1 / rank of the first answer found (0 when none is), averaged |
| `EMPTY` | share of questions with no result; an error counts as none |
| `OK/1K TOK` | answers found anywhere in the output, per 1,000 tokens of output, over all the questions |
| `MEAN TOK` | tokens of one output, on average |
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
