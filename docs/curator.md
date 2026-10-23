# The Curator

The Curator is the hub's night shift for a project. Each night, inside hours the project's admins set, the hub runs
work on one member's worker without anybody pressing Run: it reviews how the project's sessions and runs went, proposes
changes with evidence, builds the ones the owner accepted on a branch of their own, has a second agent judge each one,
and merges only the smallest kind of change, and only when every check allows it. In the morning the owner gets a brief.

Everything the Curator may do is written down in one place, the project's **charter**, and every limit that keeps it
safe is code of the hub or of the worker daemon, never a sentence in a prompt. An agent never sets the tier of its own
change, never holds a credential that can push to a default branch, and never reads the hidden checks it is judged by.

This page is the design in one place. The details live with the parts they belong to:

| Part | Where |
| --- | --- |
| the review run, proposals, the Builder, the Judge, the replays, the history run, the whole history, the memory, the merge, the ledger, the routes and the commands | `docs/hub.md`, "The Curator's review", "The Curator's changes", "The Curator's replays", "The Curator's history run", "The project's history", "The Curator's memory" and "The Curator's ledger, outcomes and circuit breaker" |
| what the daemon does in a review run, a judge run, a replay run, a history run and any run of the Curator, the run's caps | `docs/workers.md`, "A review run on the machine", "A judge run on the machine", "A replay run on the machine", "A history run on the machine", "The Curator's runs on the machine", "Runs of the night shift" |
| the leases of the Curator's runs and its own GitHub App | `docs/credentials.md`, "Which leases a run gets" |
| the morning brief, `curator_paused`, proposals in the Inbox and on Telegram | `docs/notifications.md` |

The code: `evo_agents/hub/curator.py` (the window, the night, the caps), `evo_agents/hub/review.py` (lenses and the
review prompt), `evo_agents/hub/tiers.py` (the tiers), `evo_agents/hub/judge.py` (the Builder's plan, the signs of
score hacking, the verdict, the merge rule), `evo_agents/hub/ledger.py` (the ledger and outcomes),
`evo_agents/hub/replay.py` and `evo_agents/hub/replay_suite.py` (the replays and the suite they run on),
`evo_agents/hub/history.py` and `evo_agents/hub/excerpts.py` (the history run and what its model reads),
`evo_agents/hub/history_index.py` (the words of the history for search), `evo_agents/hub/playbook.py` (the playbook),
and their routes and jobs under `evo_agents/hub/server/` (`curator.py`, `collect.py`, `proposals.py`, `changes.py`,
`replays.py`, `history.py`, `history_search.py`, `earlier.py`, `playbook.py`, `pulls.py`, `brief.py`, `ledger.py`,
`outcomes.py`, `telegram.py`).

## One night

1. **The window opens.** At the charter's `window.start`, in its time zone, the job `curator.collect` counts the
   night's figures of the project once, without any model: tool failures, failures that come from the environment,
   failed runs by cause, commands run again and again, corrections the person made, open items and stuck steps, the
   health of the knowledge graph, the runtime (tokens, the prompt cache, the context, compactions, the cost of each
   session, run and merged pull request, the time of each hook and tool) and the harness (what it puts into every
   context, by source, with the always-loaded files of each repo), and a weekly series over the whole history. The
   sessions come from their facets,
   which the job `hub.analyze_facets` makes from each transcript, digest and run that ended (`docs/hub.md`, "Session
   facets"). The graph's health comes from the measure `curator.collect` takes every 6 hours: each git source against
   its default branch on GitHub, what the builds covered, the retrieval score, and what agents got from the graph
   (`docs/hub.md`, "The graph's health").
2. **The Reviewer.** The night shift queues the night's review run on the worker on duty. Its agent reads the
   figures, then the project's whole history where they point (its sessions and runs, the words of its transcripts
   and run logs, the clusters), the traces of runs and the project's code, knowing what earlier nights
   found and the playbook, and writes findings, proposals and deltas of the playbook, each with evidence the hub can
   find. It pushes nothing and opens no pull request. Right after it comes the night's
   history run, in which a cheap model writes a facet of each session and run that has none and names the new
   clusters those facets fall into (The history run, below).
3. **The tier.** The hub computes the tier of each proposal, 0 to 3, from the charter, the paths it would change and
   its kind. Tier 2 proposals with the most evidence reach the owner's Inbox, up to the charter's
   `max_decisions_per_day`.
4. **The owner answers.** A project admin accepts, rejects or defers a proposal on the web, on the command line or on
   Telegram. Accepting one of tier 0 to 2 makes its draft plan a plan of the Curator on the hub in the same answer, with
   a change of state `planned`; accepting one of tier 3 records the answer and builds nothing.
5. **The Builder.** On a later pass of the night shift, a plan run of that plan works on the branch
   `curator/<proposal>-<slug>`, never on a default branch. Until it can, the change keeps why it waits, which the
   proposal's page and the morning brief show.
6. **The pull request and the Judge.** The hub opens the pull request (on GitLab the push opens the merge request),
   reads its diff for signs of score hacking, and queues a judge run that runs the plan's verify and the project's
   hidden checks and never reads the Builder's transcript.
7. **The replays.** A change of a skill, an AGENTS.md or a CLAUDE.md is replayed on tasks of the project's own history:
   an agent does a past step again from the code before it, once with the default branch's harness and once with the
   change's, and the step's verify says which did it (Replays, below). The Judge reads none of it.
8. **The merge.** The hub merges a tier 0 change itself, at the commit the Judge passed, only when the charter allows
   tier 0, CI is green and the repo's ruleset keeps the Curator off its default branch. Everything else stays open
   for the owner.
9. **The window closes.** The night shift queues nothing more and cancels the runs of the night still queued.
10. **The brief.** At `brief_at` the owner gets the notice `curator_brief`, on the web and on Telegram.
11. **Later.** `outcome_days` after a merge the hub counts the same figures again; a change that made them worse gets
    a revert proposal. Two jobs of a night in a row that failed or were reverted pause the night shift.

## The charter

A project has at most one charter on the hub, and every revision of it is kept (`charters`, schema 0012). Only an
admin of the project writes it, with a web session or a machine token; another member gets 403, and a worker token
gets 403 on every route of a project, the charter's included. Each revision is audited as `curator.charter`.

```sh
evo-agents hub curator charter show --project demo            # the newest revision; --revision N for an older one
evo-agents hub curator charter show --project demo --json > charter.json
evo-agents hub curator charter set charter.yaml --project demo  # YAML or JSON; - reads stdin; an admin only
evo-agents hub curator charter history --project demo         # every revision, newest first
```

What `charter show --json` prints can be written back as it is: the keys it adds (`project`, `revision`,
`updated_by`, `updated_at`, `worker_id`, `schedule_owner`) are ignored in a write. A body equal to the newest revision
writes nothing. The web shows the charter with its revisions under `/p/{project}/curator/charter`, and its form to an
admin; the form writes the whole body, the replays (`replay`) and the history run (`history`) included, but not the
owner's secrets (`git_secret`, `env_secrets`): a revision written from the form has none, so a charter that names them
is written from the command line.

A charter, as YAML:

```yaml
goals:
  - id: plan-runs
    what: A member's plan runs end done without a person stepping in.
window: {start: "22:00", end: "06:00", timezone: Asia/Ho_Chi_Minh}
worker: night-vm
night_budget_usd: 20
run_budget_usd: 5
run_minutes: 120
max_runs_per_night: 6
night_plans: [docs-sweep]
max_decisions_per_day: 5
brief_at: "07:00"
auto_merge: [0]
protected_paths: ["evo_agents/hub/migrations/**", "evo-agents:deploy/**"]
circuit_breaker: {max_failed_in_a_row: 2}
outcome_days: 7
review: {lenses: 3, days: 7, budget_usd: 2}
replay: {tasks: 3, budget_usd: 4}
history: {budget_usd: 1}
reviewer: {runtime: claude-code, model: sonnet}
builder: {runtime: claude-code, model: opus}
judge: {runtime: claude-code, model: sonnet, hidden_checks: ["python -m pytest -q tests/hub/test_judge.py"]}
env_secrets: [claude-oauth]
```

| Key | Default | What it does |
| --- | --- | --- |
| `goals` | none | the product's goals, highest first, each `{id, what}`; at most 20. Since plan loop-engine the project's goals live on the hub (`docs/hub.md`, Goals): a goal of the charter becomes a goal without a metric the first time the goals are read, its `id` kept as its alias, and the lens `product_goals` reads the project's active goals by rank. Change one with `evo-agents hub goal set` |
| `window` | required | `start` and `end` as HH:MM and an IANA `timezone`. When `end` is not after `start` the window runs past midnight; start and end may not be equal |
| `worker` | required | the worker on duty, by name (below) |
| `night_budget_usd` | required | the most a night may cost, every run of the night together |
| `run_budget_usd` | null | the most one run may cost; null gives a run what the night has left. Never above `night_budget_usd` |
| `run_max_turns` | 300 | the agent's turns in one run |
| `run_minutes` | 120 | the agent time one run may use, 10 to 720 |
| `max_runs_per_night` | 6 | runs the night shift queues in a night, the review run included |
| `night_plans` | none | the plans the night shift may run as plan runs, in the order it takes them |
| `max_decisions_per_day` | 5 | tier 2 proposals that reach the owner's Inbox a day, in the charter's time zone |
| `brief_at` | `07:00` | when the morning brief goes out, in the charter's time zone |
| `auto_merge` | none | the tiers the hub may merge by itself: only `0` is accepted |
| `protected_paths` | none | globs no change of the Curator may touch below tier 3 (Tiers, below) |
| `circuit_breaker.max_failed_in_a_row` | 2 | jobs of a night in a row that failed or were reverted before the hub pauses the night shift, 1 to 10 |
| `outcome_days` | 7 | days after a merge over which the hub counts a change's figures again, 1 to 90 |
| `review.lenses`, `review.days`, `review.budget_usd` | 3, 7, null | lenses a night looks through, days of sessions and runs the figures count (at most 30), and the most the review run may cost |
| `history.budget_usd`, `history.model` | null, null | the most the night's history run may cost, never above `night_budget_usd`; null gives the smaller of 1 USD and a quarter of `night_budget_usd`, and 0 runs none; and the model of the reviewer's runtime it runs on, null for the cheapest the worker on duty reports for that runtime (The history run, below) |
| `replay.tasks`, `replay.budget_usd` | 3, null | the tasks of the holdout split each change of a skill, an AGENTS.md or a CLAUDE.md is replayed on (0 to 20), and the most the replay runs of a night may cost together, never above `night_budget_usd`; null replays nothing (Replays, below) |
| `reviewer`, `builder`, `judge` | `claude-code`, the runtime's model | the runtime and model of each role (The three roles, below) |
| `judge.hidden_checks` | none | commands the Judge runs that no Builder sees, at most 50 |
| `git_secret` | null | the owner's `git` secret the Curator's runs use for origins not on GitHub (GitLab); without it no Builder runs there |
| `env_secrets` | none | the owner's `env` secrets the Curator's runs get, by name; they get no other |

**The worker on duty.** `worker` names one of the writer's own live workers that serves the project and takes runs
dispatched by the hub: a worker set to take runs from the web only (`dispatch_from` `web`, `docs/credentials.md`)
passes over every run the night shift queues, so the charter refuses it. A revision that keeps the worker of the
newest revision may be written by another admin. The member who owns that worker owns the project's schedule: the
night shift dispatches every run as that member (`dispatched_by`), so the rule that a worker takes only its owner's
runs holds as it is. Give that worker `--slots 1` and no credential of its owner but its leases
(`docs/credentials.md`, "What is still a risk").

**Hidden checks.** `judge.hidden_checks` are shown to the project's admins alone, and as null to anyone else. A write
with `hidden_checks: null` keeps those of the newest revision, so a charter shown without them and written back keeps
them. A worker token never reads them: only the judge run's own key opens them, inside the daemon (The three roles,
below).

## The night shift

A charter makes one schedule of kind `night_shift` (`schedules`, schema 0012). Every minute the job
`hub.fire_schedules` looks at each schedule that is not paused, under the schedule's row lock:

- outside the window, or paused, it queues nothing and cancels the runs of the schedule still queued;
- inside the window it queues at most one run, and only when no run of the schedule is queued or held, the night has
  queued fewer than `max_runs_per_night` runs, at least 0.01 USD of `night_budget_usd` is left, the schedule's owner
  still holds writer on the project, and the worker on duty is live and fit for the run;
- the run it queues is the first of these that has work: the night's review run, while the night has none; the
  night's history run, once the night's review run was queued, while the night has none, an item waits for a facet of
  the model or a group of facets has no name; the judge run of a change of the Curator that waits for one; the replay
  run of a change that waits for one, while the night's replay runs cost less than `replay.budget_usd`; the Builder of
  a change the Curator planned; a plan run of the first plan of `night_plans` that has a ready step and no active run.

The night a moment belongs to is the local date the window last opened on, so the hours after midnight count toward
the night that began the evening before. Every run the night shift queues is pinned to the worker on duty, headless,
with approval `auto`, `dispatched_via` `schedule`, and names its schedule and night (`runs.schedule_id`,
`runs.schedule_night`). Its first event says which night, charter revision, owner, worker and caps it was queued
with. Each is audited as the owner with no token: `curator.review`, `curator.history`, `curator.judge`,
`curator.replay`, `curator.build`, or `curator.dispatch` for a plan of `night_plans`.

**Caps.** Each such run carries a budget (`runs.budget`): `max_usd`, what the night has left capped by
`run_budget_usd` (and by `review.budget_usd` for the review run); `max_turns`, the charter's `run_max_turns`; and
`max_seconds`, `run_minutes` of agent time. Its timeout is `max_seconds` plus 5 minutes, so a cap stops the agent
before the timeout does. Claude Code gets `max_budget_usd` and `max_turns`; every runtime is interrupted at the time
cap; Codex reports no cost, so the time cap is the one that stops a Codex run. A run a cap stopped fails, and its log
names the cap (`docs/workers.md`, "Runs of the night shift").

**Cost.** The night's cost is that of its runs, read from their `usage`: Claude Code's `total_cost_usd`, else
opencode's `cost`. Claude Code reports `total_cost_usd` as the running total of its session, so a session counts once,
at its largest total, however many runs went on in it.

**Pause.** `evo-agents hub curator pause --project P` (an admin of the project, or the owner of one of its schedules)
pauses every schedule of the project at once and cancels the runs they queued that are still queued; a run already
held goes on until it ends, its caps stop it, or its owner cancels it (`evo-agents hub run cancel`). `resume` lets
them run again. Both are audited (`curator.pause`, `curator.resume`), and the Curator's page on the web has the same
two buttons.

```sh
evo-agents hub curator status --project demo   # the charter, the schedule, the night now, the last review and brief
evo-agents hub curator pause --project demo
evo-agents hub curator resume --project demo
```

`status` says the night shift's `state` in a word: `paused`; `running` while a run of it is queued or held;
`on_duty` inside its window with none; `idle` outside it.

## Tiers

The tier of a proposal says how far it may go without its owner. The hub computes it (`tiers.tier_of`), never an
agent:

| Tier | What happens to an accepted change |
| --- | --- |
| 0 | the night shift builds it, and the hub merges it once CI and the Judge pass, when `auto_merge` holds `0` |
| 1 | the night shift builds it and the Judge judges it; the pull request stays open for the owner |
| 2 | it reaches the owner's Inbox; once accepted, the night shift builds it and the Judge judges it, and the pull request stays open for the owner |
| 3 | it stays in the list; accepting it builds nothing, and the owner alone makes the change |

The tier is the highest of these rules, and the proposal lists in words which rule raised it:

- **Its kind.** `docs`, `memory`, `test_add` and `kg_source` are tier 0; `fix`, `refactor`, `lint`, `skill`, `cli`,
  `release_prep` and `revert` tier 1; `feature`, `api_change`, `schema_change`, `global_config`, `dependency_major`
  and `operation` tier 2; `test_loosen`, `verify_change`, `ci_change`, `credentials` and `curator_rules` always tier 3.
- **Its paths**, each `repo:path`. A protected glob of the charter, or CI, lint and test configuration and the copies
  of plans (`tiers.HARD_PATHS`), make it tier 3; a migration or schema path makes it at least tier 2; any path that is
  neither documentation nor a test makes it at least tier 1, knowledge.yaml included, except under `kg_source` (below).
- **What those paths reach** in the project's knowledge graph, when it has one: a reached path that is protected, or
  one of `HARD_PATHS`, makes it tier 3 too.
- **What the graph cannot tell**, when the project has a graph on the hub: a path it has no file for, one the latest
  measure of its health found it lacks, holds older than the default branch has it or holds though the branch deleted
  it (`kg_health`, "The graph's health" in `docs/hub.md`), or any path while the graph cannot be read, makes it at
  least tier 1. The reason names the path and why the raise by what the graph reaches cannot know what editing it
  reaches. A project with no graph on the hub keeps the tier its kind and paths give.
- **knowledge.yaml under `kg_source`.** A change of kind `kg_source` edits, of a git or harness source that
  knowledge.yaml at the root of a repo of the project already has, nothing but its `ref`, `fetch`, `refresh`,
  `include`, `exclude` or `allow`. The hub reads what the pull request does to the file key by key, at its merge base
  and at its head (`knowledge_diff.classify`), never from the proposal's words: such a change stays tier 0, and so may
  be merged by the hub when `auto_merge` holds `0`. Anything else it does to the file makes it at least tier 1, as
  knowledge.yaml is under any other kind:

  | What the pull request does to knowledge.yaml | Tier |
  | --- | --- |
  | the `ref`, `fetch`, `refresh`, `include`, `exclude` or `allow` of git and harness sources it has, at most 10 sources | 0 |
  | `policy` (its levels, locations, sinks or memo), `identifiers`, `version`, `project`, `ontology` or `bindings` | at least 1 |
  | a source's `label`, `connector`, `repo`, `path`, `branch`, `pulls`, `credentials`, `command`, `config` or any other key | at least 1 |
  | a source added, removed or moved; any change of a source of another connector, an exec command included | at least 1 |
  | the file added, deleted, not a mapping, or not readable at both ends by the hub (on GitLab it never reads it) | at least 1 |
  | a path the charter protects (`knowledge.yaml` among `protected_paths`) | 3 |

A protected glob matches the path within its repo, so `.github/workflows/**` protects that directory of every repo;
`repo:glob` protects one repo's paths. `*` matches within a directory, `**` across directories, `?` one character.

The tier only ever goes up. The hub computes it again from the paths the pull request really changes, with what the
graph says of them (read as the owner of the night shift) and what the pull request does to knowledge.yaml, when it
opens the pull request and again before a merge, and from the paths the Judge's worker read in the diff: a proposal of
docs whose pull request changes code is tier 1 from then on, and a sign of score hacking makes it tier 3.

**A harness whose replays won** (`tiers.replay_tier`). One rule lowers the tier a change merges with, never its
proposal's: a change of kind `docs`, `skill` or `global_config` whose pull request edits a harness alone (the files of
one skill directory, `.../skills/<skill>/`, its SKILL.md, documents and scripts, and AGENTS.md and CLAUDE.md files,
nothing else) is tier 0 once its replays on the holdout tasks won (`replay.verdict`, below), unless a rule made it
tier 3 (a protected path, `HARD_PATHS`, a top kind, a sign of score hacking). So a skill's script, which is code, or a
global skill, whose kind is tier 2, merges as tier 0 then. The Judge and CI must pass as for any tier 0 change, and the
hub merges it only when `auto_merge` holds `0`. Without replays that won (none, too few, or lost), or with any other
file in the diff, the change keeps the tier these rules give, and the reason it is left open says what its replays came
to. The proposal shows the tier its rules gave; the change shows 0.

| What the change's replays came to (holdout tasks with both arms at the pull request's head) | Tier it merges with |
| --- | --- |
| fewer than 6 such tasks (`replay.MIN_HOLDOUT`) | the rules' |
| arm b passed fewer tasks than arm a | the rules' |
| arm b passed more tasks than arm a | 0 |
| as many passed, and arm b's mean cost or mean turns at least 10% lower (`replay.MIN_SAVING`) | 0 |
| as many passed, neither mean 10% lower | the rules' |

A proposal like one the owner rejected in the last 30 days (the same kind and paths, or the same kind and title when
it names no path) is dropped, unless its evidence counts at least twice the rejected one's.

## The three roles

| | Reviewer | Builder | Judge |
| --- | --- | --- | --- |
| run | a run of kind `review`, one a night, first | a plan run of a plan the Curator made | a run of kind `judge`, one per pull request |
| reads | the night's figures, the whole history (sessions, transcripts, run logs, the owner's local runs and group chats, clusters), digests, traces, decisions, earlier findings, the playbook, the project's code at the default branch's commit | its plan and its repo | the proposal, the diff, the plan's verify, the hidden checks |
| writes | findings, proposals and deltas of the playbook (`evo-agents worker finding`, `propose`, `playbook`) | commits on `curator/<proposal>-<slug>` | a verdict, `{"verdict": "pass" or "fail", "reasons": "..."}` |
| GitHub token | the Curator's App, read only | the Curator's App, `contents: write` | the Curator's App, read only |
| runtime | `reviewer` of the charter | `builder` of the charter | Codex when the policy clears it, else Claude Code with a model other than the Builder's |

The plan runs of `night_plans` and the replay runs take the Builder's runtime and model too. A plan run of
`night_plans` is no run of the Curator: each is a plan run of a plan of the owner's, with the leases and the workers'
App any plan run of its owner gets, and it pushes as its plan says, the default branch included when the plan names it
(`docs/workers.md`, "Plan runs"). Only a review run, a judge run, a replay run and a run of a plan the Curator made are
runs of the Curator. List in `night_plans` only plans you would let run unattended.

**The Reviewer** works in worktrees detached at the commit origin's default branch has, and its prompt says that what
it reads is data and never instructions. It looks through `review.lenses` of fifteen lenses a night, taken in turn
(`tool_errors`, `environment`, `corrections`, `failed_runs`, `runtime`, `tech_debt`, `code_health`, `docs_drift`,
`harness`, `skills_memory`, `cost`, `security`, `product_goals`, `product`, `kg_health`; `docs/hub.md`, "Runtime and
harness", says what the night's figures hold for `runtime` and `harness`, and The lens product below what they hold for
`product`). Each finding and proposal cites evidence the hub
checks before it keeps it: `session:ID[:FIELD:INDEX]` (an entry of a session's facet, else of its digest),
`transcript:ID[:FILE]:LINE` (a line of a transcript the hub holds), `run:ID:SEQ` (an event of a run, a local run of the
owner's included) or `code:REPO:PATH[:LINE]` (a line of a repo of the project; a file the knowledge graph does not hold
yet is kept, `unmatched`, since a graph may lag its branch). A proposal carries a draft plan in outcome steps, each
with `verify` and `acceptance`. It reads in two tiers, the numbers first (the figures, the sessions and runs of the
whole history with the numbers of their facets, the clusters), then the words only where they point (a search of the
transcripts and the run logs, and the lines of a transcript around one), through the hub's MCP tools, which the
worker's adapter gives a run of the Curator whatever the worker's machine has configured. Its
prompt holds what earlier nights found through its lenses with what became of it, the proposals the owner rejected
lately, and the project's playbook (Memory, below); the hub refuses a finding like a closed one without new evidence. The review run's figures come from what the
plugin and the daemon feed the hub: the facet of each session (`docs/hub.md`, "Session facets"), made from the
transcripts the `evo-hub` plugin sends (`docs/hub.md`, "Session transcripts") or, while the hub holds none, from the
session digest its Stop hook pushes for every session of 6 messages or more (`docs/hub.md`, "Session digests"), with
every string that looks like a secret replaced before it leaves the machine; the facet of each local run of the
charter's owner; and the tool figures of each run, written when it ends and kept after its events are pruned
(`docs/workers.md`, "Tool figures").

The figures measure skills and commands in three tiers, and two lenses read them. Called: per skill, its loads
through the Skill tool and typed as `/name`, in sessions and in runs, and the loads that failed. Snags: the Bash
commands, failures by kind, interrupts and corrections from each load to the person's next turn, next to the baseline
of every session, and per program its `usage_errors` (a wrong call) and `env_errors` (the hub, the network, a timeout,
a blocked command), with the failures of commands that ran several programs counted apart. Work done: per skill, the
runs that loaded it that ended done, the steps whose verify the worker ran again at `evo-agents worker step done`, and
the runs that failed or were lost, by cause. `tool_errors` reads the programs' kinds, `skills_memory` the skills, and
when either is among the night's lenses the prompt says which tier has data: a digest that an older `evo-hub` plugin
pushed counts no skill and no kind, and a run that ended before schema 0087 recorded no skill, so a zero there means no
data, not no friction.

**The Builder** is a plan run of `curator-<proposal>-<slug>`, a plan the hub made in the same transaction as the
accepting answer: in the one repo the proposal names, every step pending. Its what, goal, context, verify and
acceptance stay as the hub made them; a write may change only the progress of its steps. No member dispatches a run of
it: the night shift alone does, at any hour, not only inside its window. Its agent holds no push credential: the daemon alone pushes, only the branch
`curator/...`, and never a default branch whatever the plan says. On GitLab the push opens the merge request. An
accepted proposal of tier 0 to 2 that has no change yet, such as one of tier 2 accepted while that tier still recorded
the answer alone, becomes its plan with `evo-agents hub curator proposal plan ID`, which only an admin of the project
may. A proposal's page on the web has a Change card once accepting it made one: the change's state, the Curator's plan
as a link to its page, the branch (a link to it on GitHub or GitLab), the pull request once the hub opened it, and the
reason the change keeps while it waits or stays open, and the tier it merges at; under it a Replays card says whether
its replays won and why, sets arm b beside arm a and lists each replay. `evo-agents hub curator proposal show ID`
prints the same, and `evo-agents hub curator replay show CHANGE` its replays. The proposal's ledger adds the verdict of
the replays a merge rests on, and each skill release.

**The Judge** runs in a worktree detached at the pull request's head. The daemon reads the diff and runs the detector of
score hacking before any code of the change runs, then the hidden checks and the verify commands as code it does not
trust, without the worker's variables or credentials, and only then starts the agent on the hub's prompt, which never
holds what the Builder wrote. The change passes only when the agent passed it, every verify command and hidden check ran
and exited 0, no sign of score hacking showed, and the commit judged is the pull request's head. For a change of kind
`kg_source` whose pull request changed knowledge.yaml, the Judge's inputs name the sources it edits and the worker runs
two more checks, whose results join the verify results and must exit 0 too: `evo-agents harness validate` in the
worktree, as code it does not trust, and a trial sync of each edited source by the daemon's own code, from the
knowledge.yaml of the commit judged into a corpus of the run's own (a git source fetched under the lock of its
checkout, and failed when the worker's nightly sync would skip it; a harness source over the worktree; nothing
pushed). On GitHub the hub
writes the verdict on the pull request as the check run `evo-agents Judge`. The Judge's inputs and verdict travel with
the run's own key, which the hub hands the daemon in the claim alone and keeps as a SHA-256 (schema 0019): code on the
worker's machine that reads the worker token neither reads a hidden check nor writes a verdict.

**Signs of score hacking** (`judge.hack_signs`): an assertion removed, a skip or xfail added, a threshold of a test
changed, an expected value changed, a golden or snapshot file changed, an assertion's failure caught, a plan's verify
or a file it runs changed, CI or the configuration of lint, types and tests changed, a lint warning silenced, `__eq__`
overloaded, an exit from a test, a protected path touched, an instruction of a skill, an AGENTS.md or a CLAUDE.md to
test or verify weakened (`instruction_weakened`: a line added that lets an agent skip the tests, the verify, lint or CI,
unless it forbids skipping them, or more lines about them removed than added), a binary file where tests, CI,
configuration or a harness live, and a diff too long to read whole. `docs/hub.md`, "The Curator's changes", lists them
all. One sign fails the change and puts its proposal at tier 3.

**The watchdog.** After each heartbeat the daemon compares what changed in the worktrees of a run of the Curator with
the charter's protected paths, and the run's agent time and cost with its caps. A protected file changed, or a cap
passed, stops the run, which fails, and its owner gets the notice `run_failed`. On Claude Code a run of the Curator uses
the Claude subscription login alone, never an API key.

### The lens product

The lens `product` (plan loop-engine, criterion a9) reads the product as the project measures it and writes proposals
that members decide; it changes no claim, baseline, goal or policy itself, and hands no work to the Loop. It takes its
turn with the other lenses: with three lenses a night, once in five nights.

- **What it reads.** The night's figures hold `product`, counted without any model as the night shift's owner reads
  the project through its hub sink: the active goals by rank with their metric and the signal key it names, their
  deadline and canvas; each signal key that is not archived with how many values the span holds, when the newest was
  measured and, for a number, the smallest, the largest and the newest; and, when the project enables the plugin
  canvas, each canvas with its claims by status, the claims at testing past their test card's deadline, its open
  proposals and its newest baseline. On its night the prompt shows this figure first. Where the project enables
  canvas, the run reads a canvas or a claim whole with `canvas__read` and a key's values with `canvas__signals`.
- **Claims.** It proposes a claim the numbers suggest and no canvas holds, or a change of a claim at the rev it read,
  with `canvas__propose` (`docs/canvas-plugin.md`): a new claim starts as an assumption with a test card, and nothing
  changes until a writer accepts it. The plugin takes the tool from a review run only on a night of this lens, and the
  proposal carries the run as its provenance. The run never edits a claim, adds evidence, proposes `validated` or
  decides a baseline: the plugin refuses each to it. A project that does not enable canvas gets no claim proposal, and
  its figure's `canvases` is null.
- **Policy.** It proposes a change of the project's autonomy policy, such as a cap or a pace that holds a goal back,
  with `builder__propose` of kind `policy` (`docs/plugins.md`, Builder), where the project enables the plugin builder.
  The proposal names the review run, has no thread, and is made for the night shift's owner while that member holds
  the writer role; its Inbox notice links to the run, and an admin of the project approves or rejects it. The run
  proposes no other kind, a goal included.

The Loop's own numbers (work items by state and by goal) join the figure once the Loop keeps work items.

## Replays

A replay measures a change of the Curator on the project's own history, the way it would meet the project's next work:
an agent does again a step of a plan done before, starting from the code as it was before that step, once with the
harness of the default branch (arm `a`) and once with the harness of the change (arm `b`), and the step's own verify
says whether each did it. Nobody writes the tasks: they are the project's steps done. `docs/hub.md`, "The Curator's
replays", has the routes and the rules; `docs/workers.md`, "A replay run on the machine", what the daemon does.

**The suite.** Every hour the job `curator.replay_suite` adds to each project's suite a task for each step of its plans
that is done, has a verify of one shell line and a repo, and names the commits it made (in its evidence, else a run of
it reported them); the base commit is the first parent of the oldest of them. A plan of the Curator gives no task. The
HMAC-SHA256 of a task's plan and step under the hub's session secret (EVO_HUB_SESSION_SECRET) puts one task in three
in the holdout split and the others in the select split, so nobody can tell from the code which tasks are held out; a
hub without the secret builds no suite. A task once in the suite never changes, whatever becomes of the secret: the
suite only grows. A change is measured on holdout tasks; the select split is there for the Curator to look at without
spending what it is measured on. The agent of a run lists that split alone, and reads neither a change's replays nor
a replay run, which all name holdout tasks; a member reads them.

**A replay run.** The night shift replays a change once it is built and not merged or closed, when its proposal is of
kind `skill` or names a skill, an AGENTS.md or a CLAUDE.md: on the first `replay.tasks` holdout tasks the worker on duty
can run, one replay run each, with the runtime and model of the Builder, and only while the night's replay runs cost
less than `replay.budget_usd`. Each arm gets a repository of its own with the task repo's history up to the base
commit and nothing after it, the change's files as its side has them, and each skill the change touches under
`.claude/skills/`, where the agent loads it; the agent gets half the run's cost and time, the step's what and verify,
and none of the hub's tools, since the hub holds the project as it is now. The worker then runs the verify on the
arm's work as code it does not trust, as the Judge runs its checks, and posts what the arm came to with the run's own
key: passed or not, cost, turns, tool calls and failed ones, the largest context, the agent's time.

**What it decides.** Each change's replays are kept with it (`curator_replays`, schema 0056) and read with
`evo-agents hub curator replay show CHANGE`, `GET .../curator/changes/{id}/replay` and the MCP tool `curator_replay`,
with their verdict at the head of the change's pull request (`replay.verdict`). The Judge never reads them. The verdict
counts the replays of the holdout split whose arm b ran at that head: with at least 6 of them, the replays won when arm
b passed more tasks than arm a, or as many with a mean cost or a mean number of turns at least 10% lower, each mean
over the tasks where both arms report it. Those numbers are constants in `evo_agents/hub/replay.py`
(`MIN_HOLDOUT`, `MIN_SAVING`), so a charter that wants a change judged by its replays sets `replay.tasks` to 6 or more.
A change of a harness alone whose replays won merges as tier 0 (Tiers, above). A change the Judge passed and the hub
left open goes back to `judged` the moment its replays win, and the next pass of `curator.changes` reads its pull
request again and merges it when everything else still holds.

**A global skill's release.** When the hub merges such a change, and the one skill directory its replays ran with is a
global skill of the hub whose latest version came from the change's repo (published with `--source-repo` naming that
repo), the merge's ledger line names the skill, and the same pass of `curator.changes` publishes it: it reads the
directory at the merge commit with the workers' App (`contents: read`), packs it as `evo-agents hub skills publish`
does, puts the bundle in the blob store as the hub's own, and records the next version of the global skill, from the
change's repo at the merge commit, published by the owner of the night shift. The version before it stays. A skill of
a project, or a global skill published from another repo, is not released. The ledger line `skill_release` says which
version the hub made and which came before, or why it made none (a symbolic link or a submodule in the directory, a
file GitHub does not hand whole, no night shift).

**Counted again.** `outcome_days` after the merge, `curator.outcomes` counts the lens's figures again as for any merged
change and keeps the replays' verdict with them. When they got worse, the hub proposes the revert as it does for any
change and, for a global skill it released, publishes the version before the hub's again at once, as a new version
holding that version's bundle, unless a version newer than the hub's came since, which it leaves as it is. Either way
a line `skill_release` says what it did.

```sh
evo-agents hub curator replay tasks --project demo            # the suite, select and holdout
evo-agents hub curator replay show 4 --project demo           # change #4: arm a against arm b, task by task
```

## The history run

The night's figures count what happened in each session without any model (`docs/hub.md`, "Session facets"); the
history run adds what a model reads in it, the way Clio reads conversations: a facet of each session and run, and
clusters of those facets, of which only those of 3 sessions or more reach the figures. `docs/hub.md`, "The Curator's
history run", has the routes and the rules; `docs/workers.md`, "A history run on the machine", what the daemon does.

**What the model writes.** For each session whose transcript the hub holds, and each run that ended (not a local run,
not a history run), that the charter's owner may read, a summary, the kind of task, its outcome, what got in the way,
the failure modes of MAST it shows (14 modes in three categories: the specification, the agents' misalignment, the
verification of the task) and the sentences in which the person corrected the agent. It reads an excerpt, not the
whole transcript: a long one is cut around its failed tool calls, the errors of the model's API and the person's
corrections, with its first and last entries, since a model reading a long trace whole finds its faults badly
(TRAIL). A facet is written once for each content of its item, and again when the item's content or the facet's
version changes.

**What it costs.** One history run a night, right after the review run, on the reviewer's runtime with the charter's
`history.model`, else the cheapest model the worker on duty reports for it (`haiku` on Claude Code), within
`history.budget_usd`: by default the smaller of 1 USD and a quarter of the night's budget. It reads the latest items
first, in batches, each batch one new session of the agent, and stops at its cap rather than failing on it: what it
could not read waits for the next night, so the number of items without a facet of the model goes down by what the
cap allows each night. An item the model gave no facet waits until the next night, and a week after 3 attempts.

**The clusters.** No embedding service groups them: the facets of the model fall into one group per kind of task and
first failure mode, which `curator.collect` counts again each night; the history run's model names each new group and
may say it is the same as another, and the two then count as one. A cluster keeps its name from night to night, so the
nights can be compared. Each has its sessions (a run counts as one), its members and a representative session, the
latest; one of fewer than 3 sessions stays out of the night's figures.

```sh
evo-agents hub curator history status --project demo     # the cap and model, the facets of the model, its runs
evo-agents hub curator history clusters --project demo   # the clusters of 3 sessions or more
```

## The whole history

What the Reviewer reads besides the night's figures is the project's whole history, on the hub and in Postgres alone
(`docs/hub.md`, "The project's history"): the sessions and runs of the facets, with their numbers, by time, member,
runtime, outcome or cluster; the words of every transcript part and every run that ended, which the job
`hub.index_history` indexes once for Postgres full-text search, the local runs of the charter's owner among them, and of
the group chats the charter's owner is in; and the lines of a transcript around one. A finding cites a line as `transcript:ID:LINE`. The prompt asks for two tiers: the
numbers to find where something goes wrong often or costs much, then the words there alone.

The Reviewer searches the group chats of the charter's owner, as the owner does with the same command, through the
sink: a chat's message is no evidence, so a finding cites the transcript line or the run event it leads to. What the
agent of a run reads lands in its run's events, which every reader of the project reads, and in its findings, so the
words of a chat the Reviewer reads can reach readers who are not in the chat. That is a known debt, kept so the
Reviewer reads the whole history; its prompt asks it to quote no chat. No other agent of a run searches a chat (a chat
run's reply would carry one chat's words into another), no agent of a run reads a replay run, and only the Reviewer
reads a local run, its owner's.

```sh
evo-agents hub curator history sessions --project demo --outcome failed   # the numbers first
evo-agents hub curator history search "rate limit" --project demo         # then the words, best first
evo-agents hub curator history lines SESSION 120 --project demo           # and the lines around one
```

On the web, the Curator's History page (`/p/{project}/curator/history`) shows the same for any member with a grant:
how many of the sessions the machines reported have their transcript on the hub (from the night's `facets`), what
waits for `evo-agents hub sessions backfill`, the facets made with and without the model, the transcripts per member,
the history runs, the search with the lines around each hit, the clusters with their latest session, and the sessions
and runs filtered by kind, outcome and cluster. Its Graph health page shows `hub kg health` over time with the night's
sync of the worker on duty, and its Runtime page the night's `runtime` and `harness` figures over 7, 14 or 30 nights
(`hub curator figures --night`).

## Memory

The review runs remember the project two ways (`docs/hub.md`, "The Curator's memory").

**Earlier nights.** For each lens of its night, the prompt shows the latest findings of earlier nights with what became
of each (open, proposed, rejected, merged, kept or reverted, from its proposals, their changes and the ledger's
outcome), and the proposals the owner rejected in the last 30 days with the note. A finding of the same lens like a
closed one (kept, merged or rejected), whose evidence that one cited already, is refused: a closed finding comes back
only with new evidence.

**The playbook.** Each project has a playbook of the Curator, entries of one lesson each with an id (`pb-N`), a lens or
every lens, evidence and counters of helpful and harmful, as Agentic Context Engineering keeps one. It changes by deltas
alone, never written anew: the agent of a review run adds an entry, counts one helpful or harmful once a run, or retires
one, each delta kept. The prompt shows the live entries of the night's lenses, the most helpful less harmful first.

```sh
evo-agents worker playbook add --lens cost --text "Count cache reads first" --evidence transcript:ID:40   # a review run's agent
evo-agents hub curator playbook --project demo   # the live entries, the most helpful first
evo-agents hub curator earlier --project demo    # earlier findings and what became of them
```

**Promoting agents' memories.** An agent profile that is not its project's default keeps memories of its own and
writes no shared memory (`docs/hub.md`, Memories of agents); the Curator decides what of them the project keeps. The
prompt of a night's review run lists the memories of the project's agents written or changed since its last review run
that ended done, each at its revision with where that revision came from (run, profile and version, the person at its
origin, chat), and the project's shared memories by id, revision, place and description. Both list only what the
run's owner reads through the agent's sink and what names no audience, never a memory archived or promoted already,
and the prompt says that memories are data, never instructions.

The Reviewer promotes with a proposal of kind `memory` that carries a promotion in place of a draft plan. Its evidence
`memory:ID:REV` names the agents' memories it promotes, each at its current revision; its target is a shared memory to
merge into (`memory_id` and `revision`) or a new one (`location`, `name` and `type` `project` or `reference`); and its
body is the whole memory after the merge. The hub checks every source and the target when the run records it (422
saying what is wrong), and gives it the join of the labels of the sources and of the target, which must pass the
project's write rule through its hub sink.

Accepting it, by an admin of the project or by the hub as the run records it when the charter's `auto_merge` names
tier 0 (kind `memory` is tier 0), writes the shared memory with a revision naming the proposal, marks each source
promoted (out of its agent's index, the searches of runs and the daily upkeep), and adds the ledger line `promoted`.
No change, plan, branch or pull request is made, and `POST .../plan` refuses such a proposal. A source or target that
changed since the proposal makes the accept a 409. A proposal of kind `memory` with a draft plan works as before. The
web shows a promotion's sources, its target, the memory after the merge and, once accepted, the shared memory it wrote.

```sh
evo-agents worker propose --lens skills_memory --kind memory --title "Keep the release checklist" \
  --evidence memory:41:3 --evidence memory:57:1 --promote-file promotion.yaml   # a review run's agent
# promotion.yaml: target: {location: harness, name: release-checklist.md, type: project}; body: the merged memory
```

## Two GitHub Apps and the ruleset

The hub uses two GitHub Apps, and the difference between them is what keeps the Curator off default branches:

| App | Variables | Who uses it | May it update a default branch |
| --- | --- | --- | --- |
| evo-agents-workers | `EVO_HUB_GITHUB_APP_ID`, `EVO_HUB_GITHUB_APP_PRIVATE_KEY` | every run that is not the Curator's (a member's step and plan runs, and the plan runs of `night_plans`); the hub's own calls for the Curator's pull requests (open, read files and CI, write the Judge's check run, merge) | yes, as a bypass actor of the ruleset |
| evo-agents-curator | `EVO_HUB_CURATOR_APP_ID`, `EVO_HUB_CURATOR_APP_PRIVATE_KEY` | every run of the Curator (Reviewer, Builder, Judge), and the hub's check of each ruleset | no |

Without the Curator's App a run of the Curator gets no GitHub token at all; it never falls back to the workers' App.
The hub's own calls with the workers' App ask for a token of one repo with the permissions that one call needs, and
revoke it after the call.

To let the night shift build on a GitHub repo:

1. Install evo-agents-curator on the repo, with Contents read and write and Metadata read.
2. Add a branch ruleset that targets the default branch, with enforcement Active, the rule that restricts updates,
   and the rule that requires the status checks CI runs. Put evo-agents-workers and the repo's admins in its bypass
   list, never evo-agents-curator.
3. Check it: `evo-agents hub curator protection --check REPO --project demo` (an admin of the project), or let the
   hub do it: the job `curator.changes` checks the ruleset of a repo a planned change waits for on its own.

The check asks GitHub, with a token of the Curator's App: the repo's default branch, the rules that apply to it, and
for each ruleset with a rule of type `update`, whether it is `active`, whether `current_user_can_bypass` is `never`
for the Curator's App, and whether its bypass actors leave that App out. Anything else, a field missing included, is a
repo the Curator is not kept off. The hub keeps what it found (`curator_repo_checks`). The job `curator.changes`
checks, on its own and with an empty `checked_by`, the ruleset of each repo a planned change waits for: at its next
pass when the repo has no check within 2 days, and every hour while the last check did not pass and the change still
waits. It checks every other repo checked before once a day, and the hub checks again each time a Builder asks for its
leases. A hub without the Curator's App asks GitHub nothing, and the change says so. The hub never creates or changes
a ruleset: a change whose repo's check did not pass keeps, as its reason, which repo, why, and that its owner has to set
it up as above. A Builder runs on a GitHub repo only after a check within 2 days that passed; a repo whose ruleset no
longer keeps the Curator off gets no token.

**Merging tier 0.** The hub merges a pull request with the workers' App, as a merge commit, at the head the Judge
passed, only when all of these hold: the change is tier 0 and `auto_merge` holds `0`; the Judge passed it; the repo's
ruleset still keeps the Curator off, checked again then; the pull request is open into the default branch; its files
show no sign and no protected path; and CI is green. Green means each check the default branch's active rulesets
require concluded success (as a check run of the App the rule names, when it names one), every other check run ended
success, neutral or skipped, and the commit statuses are success. A ruleset that requires no check, or a head commit
whose message asks CI to skip it, leaves the pull request open. The hub waits up to 6 hours while CI runs, and merges
nothing while the night shift is paused. A tier 1 or 2 change and a refusal stay open for the owner with the reason, as
does a GitLab merge request, unless the project's autonomy policy puts its repo in the merge queue (GitLab, below).

**GitLab.** The charter's `git_secret` names the owner's git secret for the repo's origin, which should hold the
Developer role: GitLab then keeps it off a protected default branch. A Builder pushes with push options that open the
merge request, and the Judge judges it as on GitHub. The Curator never merges it itself. When the project's autonomy
policy has the repo in mode `auto` and a forge secret of the project covers its origin, a merge request the Judge passed
goes to the hub's merge queue, whatever its tier: the queue gates it again, merges it in its turn with the sha its
pipeline passed on, and follows it after the merge, revert and rollback included (`docs/merge-queue.md`). Without a
policy, or with the repo in mode `off`, it stays open for the owner with the reason, as before.

```sh
evo-agents hub curator protection --project demo              # the project's repos and the last check of each
evo-agents hub curator changes --project demo                 # what each accepted proposal became
evo-agents hub curator proposal plan 10 --project demo        # an accepted proposal without a change becomes a plan
```

## Telegram

The hub has one Telegram bot. A member links a private chat from the Inbox with a one-time code that lives 10
minutes, sent as `/start <code>`, and unlinks with `/stop` or on the web. Only a web session links a chat, and the
chat lives as long as that session. Decisions and tier 2 proposals arrive with one button per answer (for a proposal:
Accept, Reject, Defer 7 days) and a button that opens the web; the morning brief and `curator_paused` arrive as plain
notices. The webhook `/v1/telegram/webhook` refuses any call without the secret header before it reads the body.

A project whose hub sink is cleared at `customer` or above, or at the location `domestic-only`, sends Telegram only
its name, the kind of thing that waits and the link, with no answer button; a project whose ladder cannot be read
that way is treated the same. No message holds a decision's context, a proposal's summary, a diff or a token.
`docs/notifications.md`, "Telegram", has the rest.

## The brief, the ledger and the circuit breaker

**The morning brief** goes to the schedule's owner at `brief_at`, once a local day: the night's runs and cost against
its budget, its review run, merges into a default branch, runs waiting for approval, the Curator's changes still
planned with why each waits, decisions and proposals waiting, and the last heartbeat of the worker on duty
(`curator_briefs`, schema 0015). For a project whose Loop is on (plan loop-engine), it has the Loop's part too: the
runs the Loop handed out and the work items it finished in the 24 hours before the brief, and those waiting for a
person by reason, with a link to the decision board (`docs/hub.md`, "The decision board"); a project whose Loop is off
has none, and a project whose Loop is on with no brief of the Curator gets that part alone as the notice `loop_brief`
at 07:00. A planned change's reason is the one the night shift wrote the last time it passed
over it: the ruleset not checked or not keeping the Curator off, the owner's git secret missing, a run of its plan
still active, no step ready, a worker on duty that cannot build it, or its plan gone from the hub. `evo-agents hub
curator changes` shows it too.

**The ledger** (`curator_ledger`, schema 0018) has one line for each thing that happened to a proposal, only ever
added to: who acted (`curator`, `agent` or `user`), what happened, the commit, the default branch before and after,
the figures that set it off, the Judge's verdict, the pull request and its merge. `outcome_days` after a merge the
job `curator.outcomes` counts the same figures again: `keep` when none got worse, `revert` when one got worse and none
better, `unclear` otherwise or when there was too little to count. A `revert` makes the hub propose the revert
itself, a tier 1 proposal of kind `revert` whose plan reverts the merge commit, which reaches the Inbox and Telegram
whatever room the day has left. The hub's merge of a change whose replays won keeps their verdict under the line's
`details.replay`, and a global skill it releases under `details.release`; the line `skill_release` (schema 0058) is the
version it published from the merge, or the one before published again after a revert.

**The circuit breaker** pauses every schedule of the project once `circuit_breaker.max_failed_in_a_row` jobs of one
night in a row failed or were reverted, cancels what they queued, and sends the owner the notice `curator_paused`. A run
that ended done breaks the row. `evo-agents hub curator resume` lets the night shift run again and starts the count
again.

```sh
evo-agents hub curator proposal list --state open --tier 2 --project demo
evo-agents hub curator proposal show 7 --project demo
evo-agents hub curator proposal accept 7 --note "go" --project demo
evo-agents hub curator proposal defer 9 --days 14 --project demo
evo-agents hub curator findings --run 41 --project demo
evo-agents hub curator figures --night 2026-10-08 --project demo
evo-agents hub kg health --project demo
```

## State on the hub

| Schema | Adds |
| --- | --- |
| 0012 | `charters`, `schedules`, and on `runs` the schedule, the night and the budget; `dispatched_via` `schedule` |
| 0013 | `session_digests`, `run_tool_stats` |
| 0014 | runs of kind `review`, `workers.run_kinds`, `curator_figures`, `findings`, `proposals`, notifications of kind `proposal` |
| 0015 | `curator_briefs`, `telegram_links`, the notice `curator_brief`, `notification_deliveries.external_id` |
| 0016 | the web session a Telegram chat and its link code belong to |
| 0017 | runs of kind `judge`, `curator_changes`, `curator_repo_checks` |
| 0018 | `curator_ledger`, revert proposals, `schedules.pause_reason`, the notice `curator_paused` |
| 0019 | `curator_changes.judge_key` |
| 0054 | `session_facets`, the deterministic facet of each session and each run that ended |
| 0055 | `kg_builds.coverage`, `kg_source_files`, `kg_health`: the health of a project's graph; the lens `kg_health` |
| 0056 | runs of kind `replay`, `replay_tasks`, `curator_replays`: the suite of replay tasks and the replays of the changes |
| 0057 | the kind `kg_source`, `curator_changes.knowledge`: what a pull request does to knowledge.yaml, key by key |
| 0058 | the ledger line `skill_release`: a global skill the hub published from a merge whose replays won, or published again |
| 0059 | runs of kind `history`, the facet of the model on `session_facets`, `history_runs`, `facet_clusters`: the history run and the clusters of its facets |
| 0060 | `history_lines`, `history_indexed`: the words of the history for search; `curator_playbook`, `curator_playbook_deltas`: the playbook and its deltas |
| 0061 | the lenses `runtime` and `harness` |
| 0094 | the lens `product` |

`docs/hub.md`, "Workers and runs", says what going back from each one does. The jobs are `hub.fire_schedules`,
`curator.collect`, `curator.brief`, `curator.changes`, `curator.outcomes`, `curator.replay_suite` and
`hub.analyze_facets` (`docs/hub.md`, "Worker and queue").

## What the Curator does not do

- It never pushes to, or merges into, a default branch with its own credentials, and it merges nothing above tier 0.
- It merges nothing on GitHub for a project whose charter leaves `auto_merge` empty. On GitLab it merges nothing
  itself: the hub's merge queue merges its merge request when the project's autonomy policy has the repo in mode `auto`
  (`docs/merge-queue.md`), and it stays open otherwise.
- It does not turn a product goal into plans on its own. That step, the third phase that the plans curator-agent,
  history-driven-curator and hub-plan-authoring left out of their scope, is the Loop's (plan loop-engine,
  `docs/loop.md`): deterministic code of the hub that hands an approved use case's frame task to an author run and the
  plan it writes to a plan run. The Curator stays the night shift that tunes the harness and the runtime; its lens
  `product` only proposes claims and changes of the policy, and hands no work to the Loop. Its replays decide one
  thing alone: a change of a harness alone whose replays won merges as tier 0; any other change keeps its tier
  whatever its replays came to.
- It publishes no skill of a project, and no global skill it did not merge or that came from another repo.
- It reads no transcript on a member's machine: the evo-hub plugin sends them to the hub, with every string that looks
  like a secret replaced on the machine, and the Reviewer reads what the hub holds through the hub's tools.
- It does not deploy, run a migration on real data or publish a release: those stay decisions of a person, and the
  kind `operation` is tier 2.
