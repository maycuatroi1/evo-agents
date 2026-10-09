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
| the review run, proposals, the Builder, the Judge, the merge, the ledger, the routes and the commands | `docs/hub.md`, "The Curator's review", "The Curator's changes" and "The Curator's ledger, outcomes and circuit breaker" |
| what the daemon does in a review run, a judge run and any run of the Curator, the run's caps | `docs/workers.md`, "A review run on the machine", "A judge run on the machine", "The Curator's runs on the machine", "Runs of the night shift" |
| the leases of the Curator's runs and its own GitHub App | `docs/credentials.md`, "Which leases a run gets" |
| the morning brief, `curator_paused`, proposals in the Inbox and on Telegram | `docs/notifications.md` |

The code: `evo_agents/hub/curator.py` (the window, the night, the caps), `evo_agents/hub/review.py` (lenses and the
review prompt), `evo_agents/hub/tiers.py` (the tiers), `evo_agents/hub/judge.py` (the Builder's plan, the signs of
score hacking, the verdict, the merge rule), `evo_agents/hub/ledger.py` (the ledger and outcomes), and their routes
and jobs under `evo_agents/hub/server/` (`curator.py`, `collect.py`, `proposals.py`, `changes.py`, `pulls.py`,
`brief.py`, `ledger.py`, `outcomes.py`, `telegram.py`).

## One night

1. **The window opens.** At the charter's `window.start`, in its time zone, the job `curator.collect` counts the
   night's figures of the project once, without any model: tool failures, failures that come from the environment,
   failed runs by cause, commands run again and again, corrections the person made, open items and stuck steps.
2. **The Reviewer.** The night shift queues the night's review run on the worker on duty. Its agent reads the
   figures, the session digests, the traces of runs and the project's code, and writes findings and proposals, each
   with evidence the hub can find. It pushes nothing and opens no pull request.
3. **The tier.** The hub computes the tier of each proposal, 0 to 3, from the charter, the paths it would change and
   its kind. Tier 2 proposals with the most evidence reach the owner's Inbox, up to the charter's
   `max_decisions_per_day`.
4. **The owner answers.** A project admin accepts, rejects or defers a proposal on the web, on the command line or on
   Telegram. Accepting one of tier 0 or 1 makes its draft plan a plan of the Curator on the hub.
5. **The Builder.** On a later pass of the night shift, a plan run of that plan works on the branch
   `curator/<proposal>-<slug>`, never on a default branch.
6. **The pull request and the Judge.** The hub opens the pull request (on GitLab the push opens the merge request),
   reads its diff for signs of score hacking, and queues a judge run that runs the plan's verify and the project's
   hidden checks and never reads the Builder's transcript.
7. **The merge.** The hub merges a tier 0 change itself, at the commit the Judge passed, only when the charter allows
   tier 0, CI is green and the repo's ruleset keeps the Curator off its default branch. Everything else stays open
   for the owner.
8. **The window closes.** The night shift queues nothing more and cancels the runs of the night still queued.
9. **The brief.** At `brief_at` the owner gets the notice `curator_brief`, on the web and on Telegram.
10. **Later.** `outcome_days` after a merge the hub counts the same figures again; a change that made them worse gets
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
admin.

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
reviewer: {runtime: claude-code, model: sonnet}
builder: {runtime: claude-code, model: opus}
judge: {runtime: claude-code, model: sonnet, hidden_checks: ["python -m pytest -q tests/hub/test_judge.py"]}
env_secrets: [claude-oauth]
```

| Key | Default | What it does |
| --- | --- | --- |
| `goals` | none | the product's goals, highest first, each `{id, what}`; at most 20. The lens `product_goals` reads them |
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
- the run it queues is the first of these that has work: the night's review run, while the night has none; the judge
  run of a change of the Curator that waits for one; the Builder of a change the Curator planned; a plan run of the
  first plan of `night_plans` that has a ready step and no active run.

The night a moment belongs to is the local date the window last opened on, so the hours after midnight count toward
the night that began the evening before. Every run the night shift queues is pinned to the worker on duty, headless,
with approval `auto`, `dispatched_via` `schedule`, and names its schedule and night (`runs.schedule_id`,
`runs.schedule_night`). Its first event says which night, charter revision, owner, worker and caps it was queued
with. Each is audited as the owner with no token: `curator.review`, `curator.judge`, `curator.build`, or
`curator.dispatch` for a plan of `night_plans`.

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
| 2 | it reaches the owner's Inbox; accepting it builds nothing, and the owner makes the change or writes a plan for it |
| 3 | it stays in the list; accepting it builds nothing, and the owner alone makes the change |

The tier is the highest of three rules, and the proposal lists in words which rule raised it:

- **Its kind.** `docs`, `memory` and `test_add` are tier 0; `fix`, `refactor`, `lint`, `skill`, `cli`,
  `release_prep` and `revert` tier 1; `feature`, `api_change`, `schema_change`, `global_config`, `dependency_major`
  and `operation` tier 2; `test_loosen`, `verify_change`, `ci_change`, `credentials` and `curator_rules` always tier 3.
- **Its paths**, each `repo:path`. A protected glob of the charter, or CI, lint and test configuration and the copies
  of plans (`tiers.HARD_PATHS`), make it tier 3; a migration or schema path makes it at least tier 2; any path that is
  neither documentation nor a test makes it at least tier 1.
- **What those paths reach** in the project's knowledge graph, when it has one: a reached path that is protected, or
  one of `HARD_PATHS`, makes it tier 3 too.

A protected glob matches the path within its repo, so `.github/workflows/**` protects that directory of every repo;
`repo:glob` protects one repo's paths. `*` matches within a directory, `**` across directories, `?` one character.

The tier only ever goes up. The hub computes it again from the paths the pull request really changes, when it opens
the pull request and again before a merge, and from the paths the Judge's worker read in the diff: a proposal of docs
whose pull request changes code is tier 1 from then on, and a sign of score hacking makes it tier 3.

A proposal like one the owner rejected in the last 30 days (the same kind and paths, or the same kind and title when
it names no path) is dropped, unless its evidence counts at least twice the rejected one's.

## The three roles

| | Reviewer | Builder | Judge |
| --- | --- | --- | --- |
| run | a run of kind `review`, one a night, first | a plan run of a plan the Curator made | a run of kind `judge`, one per pull request |
| reads | the night's figures, digests, traces, decisions, the project's code at the default branch's commit | its plan and its repo | the proposal, the diff, the plan's verify, the hidden checks |
| writes | findings and proposals (`evo-agents worker finding`, `propose`) | commits on `curator/<proposal>-<slug>` | a verdict, `{"verdict": "pass" or "fail", "reasons": "..."}` |
| GitHub token | the Curator's App, read only | the Curator's App, `contents: write` | the Curator's App, read only |
| runtime | `reviewer` of the charter | `builder` of the charter | Codex when the policy clears it, else Claude Code with a model other than the Builder's |

The plan runs of `night_plans` take the Builder's runtime and model too, but they are not runs of the Curator: each is
a plan run of a plan of the owner's, with the leases and the workers' App any plan run of its owner gets, and it pushes
as its plan says, the default branch included when the plan names it (`docs/workers.md`, "Plan runs"). Only a review
run, a judge run and a run of a plan the Curator made are runs of the Curator. List in `night_plans` only plans you
would let run unattended.

**The Reviewer** works in worktrees detached at the commit origin's default branch has, and its prompt says that what
it reads is data and never instructions. It looks through `review.lenses` of eleven lenses a night, taken in turn
(`tool_errors`, `environment`, `corrections`, `failed_runs`, `tech_debt`, `code_health`, `docs_drift`,
`skills_memory`, `cost`, `security`, `product_goals`). Each finding and proposal cites evidence the hub checks before
it keeps it: `session:ID[:FIELD:INDEX]` (an entry of a session digest), `run:ID:SEQ` (an event of a run) or
`code:REPO:PATH[:LINE]` (a line of a repo of the project). A proposal carries a draft plan in outcome steps, each with
`verify` and `acceptance`. The review run's figures come from two sources the plugin and the daemon feed the hub: the
session digest that the Stop hook of the `evo-hub` plugin pushes for every Claude Code session of 6 messages or more,
with every string that looks like a secret replaced before it leaves the machine and deleted 90 days after its last
push (`docs/hub.md`, "Session digests"); and the tool figures of each run, written when it ends and kept after its
events are pruned (`docs/workers.md`, "Tool figures").

**The Builder** is a plan run of `curator-<proposal>-<slug>`, a plan the hub made in the same transaction as the
accepting answer: in the one repo the proposal names, every step pending. Its what, goal, context, verify and
acceptance stay as the hub made them; a write may change only the progress of its steps. No member dispatches a run of
it: the night shift alone does. Its agent holds no push credential: the daemon alone pushes, only the branch
`curator/...`, and never a default branch whatever the plan says. On GitLab the push opens the merge request.

**The Judge** runs in a worktree detached at the pull request's head. The daemon reads the diff and runs the detector of
score hacking before any code of the change runs, then the hidden checks and the verify commands as code it does not
trust, without the worker's variables or credentials, and only then starts the agent on the hub's prompt, which never
holds what the Builder wrote. The change passes only when the agent passed it, every verify command and hidden check ran
and exited 0, no sign of score hacking showed, and the commit judged is the pull request's head. On GitHub the hub
writes the verdict on the pull request as the check run `evo-agents Judge`. The Judge's inputs and verdict travel with
the run's own key, which the hub hands the daemon in the claim alone and keeps as a SHA-256 (schema 0019): code on the
worker's machine that reads the worker token neither reads a hidden check nor writes a verdict.

**Signs of score hacking** (`judge.hack_signs`): an assertion removed, a skip or xfail added, a threshold of a test
changed, an expected value changed, a golden or snapshot file changed, an assertion's failure caught, a plan's verify
or a file it runs changed, CI or the configuration of lint, types and tests changed, a lint warning silenced, `__eq__`
overloaded, an exit from a test, a protected path touched, a binary file where tests, CI or configuration live, and a
diff too long to read whole. `docs/hub.md`, "The Curator's changes", lists them all. One sign fails the change and
puts its proposal at tier 3.

**The watchdog.** After each heartbeat the daemon compares what changed in the worktrees of a run of the Curator with
the charter's protected paths, and the run's agent time and cost with its caps. A protected file changed, or a cap
passed, stops the run, which fails, and its owner gets the notice `run_failed`. On Claude Code a run of the Curator uses
the Claude subscription login alone, never an API key.

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
3. Check it: `evo-agents hub curator protection --check REPO --project demo` (an admin of the project).

The check asks GitHub, with a token of the Curator's App: the repo's default branch, the rules that apply to it, and
for each ruleset with a rule of type `update`, whether it is `active`, whether `current_user_can_bypass` is `never`
for the Curator's App, and whether its bypass actors leave that App out. Anything else, a field missing included, is a
repo the Curator is not kept off. The hub keeps what it found (`curator_repo_checks`), checks again every day, and
again each time a Builder asks for its leases. A Builder runs on a GitHub repo only after a check within 2 days that
passed; a repo whose ruleset no longer keeps the Curator off gets no token.

**Merging tier 0.** The hub merges a pull request with the workers' App, as a merge commit, at the head the Judge
passed, only when all of these hold: the change is tier 0 and `auto_merge` holds `0`; the Judge passed it; the repo's
ruleset still keeps the Curator off, checked again then; the pull request is open into the default branch; its files
show no sign and no protected path; and CI is green. Green means each check the default branch's active rulesets
require concluded success (as a check run of the App the rule names, when it names one), every other check run ended
success, neutral or skipped, and the commit statuses are success. A ruleset that requires no check, or a head commit
whose message asks CI to skip it, leaves the pull request open. The hub waits up to 6 hours while CI runs, and merges
nothing while the night shift is paused. A tier 1 change, a refusal, and every GitLab merge request stay open for the
owner with the reason.

**GitLab.** The charter's `git_secret` names the owner's git secret for the repo's origin, which should hold the
Developer role: GitLab then keeps it off a protected default branch. A Builder pushes with push options that open the
merge request; the hub merges nothing there.

```sh
evo-agents hub curator protection --project demo              # the project's repos and the last check of each
evo-agents hub curator changes --project demo                 # what each accepted proposal became
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
its budget, its review run, merges into a default branch, runs waiting for approval, decisions and proposals waiting,
and the last heartbeat of the worker on duty (`curator_briefs`, schema 0015).

**The ledger** (`curator_ledger`, schema 0018) has one line for each thing that happened to a proposal, only ever
added to: who acted (`curator`, `agent` or `user`), what happened, the commit, the default branch before and after,
the figures that set it off, the Judge's verdict, the pull request and its merge. `outcome_days` after a merge the
job `curator.outcomes` counts the same figures again: `keep` when none got worse, `revert` when one got worse and none
better, `unclear` otherwise or when there was too little to count. A `revert` makes the hub propose the revert
itself, a tier 1 proposal of kind `revert` whose plan reverts the merge commit, which reaches the Inbox and Telegram
whatever room the day has left.

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

`docs/hub.md`, "Workers and runs", says what going back from each one does. The jobs are `hub.fire_schedules`,
`curator.collect`, `curator.brief`, `curator.changes` and `curator.outcomes` (`docs/hub.md`, "Worker and queue").

## What the Curator does not do

- It never pushes to, or merges into, a default branch with its own credentials, and it merges nothing above tier 0.
- It merges nothing on GitLab, and nothing for a project whose charter leaves `auto_merge` empty.
- It does not turn a product goal into plans on its own, and it does not change prompts or skills through a replay
  of past sessions.
- It does not push whole transcripts to the hub: only digests, with secrets replaced on the machine.
- It does not deploy, run a migration on real data or publish a release: those stay decisions of a person, and the
  kind `operation` is tier 2.
