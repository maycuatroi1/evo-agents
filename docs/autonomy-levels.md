# Autonomy levels

The hub sorts what a run may do into twelve classes of action, gives each class a mode, and bundles the modes into
six levels, A0 to A5. A person picks the level they prefer; the org chart's ceilings (`docs/org-chart.md`), the
project policy and the agent profile's seat bound it. The catalog is `evo_agents.hub.autonomy_levels`, standard
library only.

## Classes, modes and levels

| Class | Action |
|---|---|
| K1 | read code, documents, the graph and logs within the grants |
| K2 | edit and run commands in the run's workspace |
| K3 | commit and push the run's branch, never forced |
| K4 | open a pull or merge request, comment, label |
| K5 | merge into the default or target branch |
| K6 | deploy, publish, tag a release |
| K7 | use a credential that writes or reaches a real environment |
| K8 | create, rotate or revoke a credential |
| K9 | delete or overwrite data for good |
| K10 | send out on a person's behalf |
| K11 | spend money beyond the runtime's allowance |
| K12 | change grants, ceilings, policies or profiles |

The modes, strictest first: `deny`, `ask` (open a decision and wait), `notify` (act, then send a notice), `auto` (act;
the ledger records it). K1 to K4 and K7 never take `ask`.

| Class | A0 | A1 | A2 | A3 | A4 | A5 |
|---|---|---|---|---|---|---|
| K1 | auto | auto | auto | auto | auto | auto |
| K2, K3, K4 | deny | auto | auto | auto | auto | auto |
| K5 | deny | ask | notify, ask on the merge queue's risk paths | auto | auto | auto |
| K6 | deny | deny | ask | notify | auto | auto |
| K7 | deny | auto, only the credentials that push the run's branch | auto, those and the secrets labelled for a test environment | auto | auto | auto |
| K8, K9 | deny | deny | deny | ask | notify | auto |
| K10 | deny | deny | ask | ask | notify | auto |
| K11 | deny | deny | deny | ask | notify, within budget | auto, within budget |
| K12 | deny | deny | deny | deny | ask | auto, never above the writer's own level |

A cell's condition is written after a colon: `notify:ask_on_risk_paths` (K5), `auto:run_branch` and
`auto:test_secrets` (K7). K11 always acts within budget and K12 never above the writer's own level.

## The effective mode

Each class takes the strictest mode of seven sources, and the hub names the source that set it:

1. the person's preference (`pref:<login>`); a person who set none prefers A5;
2. the ceiling of their primary seat (`seat:<id>`);
3. the ceilings of its unit and every unit above it (`unit:<slug>`);
4. the project policy of the repo, when one is named (`policy:<project>/<repo>`): `mode: off` caps K5 at ask,
   `prod_release: manual` caps K6 at ask;
5. the seat of the agent profile and its units, when one is named;
6. the credential shared with the run (`credential_share`), which limits nothing yet;
7. the instance (`instance`), which limits no class; the floors S1 to S5 hold whatever the mode, and K9 waits for a
   backup made within 24 hours.

On a tie the first source in that order is named. The effective level is the lowest level a source sets; the org
chart's effective ceiling is this same computation over sources 2 and 3. The environments and workers a run may use
are the intersection of the lists the ceilings of sources 2, 3 and 5 give; a ceiling without a list sets no limit.

A preference above the ceilings is stored as written, and the ceilings still decide. A ceiling never allows a class
above the writer's own effective mode on it: a lead whose unit caps K6 at ask cannot give a cohort below K6 notify.

## Runs: principal, snapshot and gates

Every run the hub queues (all kinds but local) acts for a principal, whose effective autonomy bounds it:

| Run | Principal |
|---|---|
| step, plan, author or task run | the member who dispatched it (a task run: its agent type's run_as) |
| chat run | the person who wrote the message that mentions the agent |
| chat run of an agent type with a run_as | that writer, bounded by the run_as person too: each class takes the stricter of both |
| run the hub makes on its own (night shift, release cycle, triage) | the sponsor of the agent profile's live seat |
| run of the Loop | the owner of the work item's goal |
| agent without a seat | the run's owner, as before |
| run an agent asks for (a mention by an agent) | the principal of the asking run, never wider than its snapshot |

When the hub queues a run it writes `runs.principal_id` and the snapshot `runs.autonomy`: the level, each class with
its mode, source and condition, the environments and workers allowed, `restricted` (a class below auto, the project
policy aside), `also` (logins whose autonomy bounds it besides the principal) and `taken_at`. A next attempt and a
resumed run copy both. A gate takes the stricter of the snapshot and the value now (`run_autonomy.recheck`, which the
claim calls), and writes a stricter one back with `tightened_at`: a lower level reaches a running run at its next gate, a
higher one only the runs queued after it.

A worker claims a run only when its name is among the snapshot's workers and the host or an environment it takes is
among its environments; a restricted run goes only to a daemon that reports the feature `autonomy`, on claude-code.
A run at A5, or one without a snapshot, is claimed as before. `GET /v1/projects/<p>/runs/<id>` and
`evo-agents hub run show --json` add `principal` and `autonomy`.

## Enforcement: lease, merge, deploy and routes

The hub holds five classes where it acts for a run, each read for the run's principal as the stricter of the snapshot
and the value now. A run without a snapshot, and a principal at A5, act as before.

**K7 at the lease** (`evo_agents/hub/server/lease_autonomy.py`). The credentials a run is leased follow K7; the
runtime's own sign-in (an env secret of a model provider) is no part of it, so every run that runs gets it.

| K7 | GitHub token | git secret | env secrets |
|---|---|---|---|
| auto, notify | as before | as before | as before |
| auto:run_branch | writes only for a repo whose ruleset keeps the hub's App off its default branch (`pulls.check_ruleset`); a token for that repo alone that reads for every other | only for an origin on GitLab whose default branch no one may push to (gate 0 of the merge queue) | the runtime's alone |
| auto:test_secrets | as run_branch | as run_branch | the runtime's, and those labelled `environment=test` |
| deny | a token that reads, for each repo alone | none | the runtime's alone |

A git secret withheld leaves its repo on GitHub to the App, and any other repo missing with its reason. A repo on
GitHub whose ruleset cannot keep the App off its default branch gets a token that reads only, with the reason in the
audit row: the hub cannot give a token that writes to a run branch alone there. A lease that withheld anything writes
`autonomy.refused` naming the class, the mode, the source and what it kept back. `hub secret set --environment test`
labels an env secret (schema 0107 adds `secrets.environment`).

**K5 at the merge** (`evo_agents/hub/server/autonomy_gates.py`): the GitLab merge queue (`merge_gate.ready`) and the
Curator's tier merge (`changes`). **K6 at the deploy and the release**: the PROD deploy of the policy (`post_merge`) and
the release schedule when it lands a release (`release_land`).

| Mode | What the hub does |
|---|---|
| auto | acts |
| notify | acts, then sends the principal the notice `autonomy` (audit `autonomy.notified`) |
| ask | opens a decision of category `autonomy` to the principal (audit `autonomy.asked`) and acts once they approve; a merge request waits judging, a pull request stays open, a deploy or a release waits. Refused, the act is left undone; a decision cancelled or expired with its run is asked again |
| deny | leaves the act undone with the reason; a merge request of the queue is blocked |

A K5 cell `notify:ask_on_risk_paths` asks when the merge request touches a risk path. An ask that a project policy sets
(a repo in mode off, `prod_release` manual: source `policy:...`) is left to the flows that already hold that policy, so
the gate acts as before. Only the run's principal answers a decision of category `autonomy`.

**K8 and K12 at the routes** (`evo_agents/hub/server/route_autonomy.py`). A write with the credential of a run (the
agent of a run on `/mcp`) to the routes of secrets and accounts (K8) or of grants, project and autonomy policies,
agent profiles, the org and its ceilings (K12) reads the principal's mode: deny is 403 naming the class, the mode and
the source; ask is 403 telling the agent to ask its principal; notify and auto leave the route's own rules to decide.
Every refusal writes `autonomy.refused`.

## The hook of autonomy on the worker

The claim gives the worker the run's card (`RunSpec.autonomy`: the mode and the source of each class, the categories of
a decision). For every run of Claude Code that has one, the worker adds a `PreToolUse` hook next to the refusal of
`claude` calls (`evo_agents/worker/autonomy_hook.py`). It reads each Bash command against a table of patterns
(`evo_agents/worker/command_classes.py`): K9 (a forced push or a deleted remote branch, `DROP`, `TRUNCATE` or a
`DELETE` without `WHERE` given to an SQL client, `rm -rf` outside the worktree and the temp dir, `terraform destroy`,
`kubectl delete`, the recursive deletion of a bucket), K5 (`gh pr merge`, `glab mr merge`, a push to a default branch),
K6 (a push of a tag `v*`, `gh release create`, `glab release create`), K8 (`evo-agents hub secret` and `hub account`
writes), K10 (`gh issue|pr create` or `glab issue|mr create` on a repo not the run's) and K12 (`evo-agents hub
admin|policy|agent|org|autonomy` writes).

| Mode | What the hook does |
|---|---|
| deny | refuses, naming the class, the mode and the source |
| ask | refuses, telling the agent to run `evo-agents worker ask --category C` and, once answered, the command again as `EVO_DECISION=<id> COMMAND`; the hub lets it through only for an answered decision of the run whose category is the class's |
| notify | asks the hub (`POST /v1/worker/runs/{id}/autonomy-acts`), which sends the principal the notice `autonomy`, then lets the command run and writes an event of the run |
| auto | lets the command run and writes an event of the run |

The hub answers the mode now: when it reads a stricter mode than the card, the hook refuses. When the hub does not
answer within 10 seconds, a command in notify runs.

**The floor S2.** A command of K9 runs only after a backup made within the last 24 hours, for every principal, A5 too:
the agent makes it, then records it with `evo-agents worker backup --target WHAT --location WHERE [--made-at ISO]
[--restore COMMAND]`. The hub writes it to the run's log and to the audit (`autonomy.backup`); the worker keeps it in the
run's `backups.jsonl`, where the hook reads it. A time in the future is refused.

**The floor S3 at a push.** Every push of the worker (`gitops._send`) first reads the commits it would send that the
remote lacks and refuses one whose message or added lines hold the value of one of the run's leases
(`evo_agents/worker/lease_leaks.py`). An agent's command, which holds no value, asks the run's socket (`op leaks`),
which answers the commit alone. The refusal names the commit, never the value.

**A second layer.** The hook reads the command's text. A command in base64, in a subshell, in a script the agent wrote,
or through a tool other than Bash gets past it, and only Claude Code runs it so far. The hub's gates (the lease, the
merge, the deploy, the routes) are what hold; the hook catches the plain cases early and leaves a trace of them.

## Commands

| Command | What it does |
|---|---|
| `evo-agents hub autonomy show [--login L] [--agent H --project P] [--repo R] [--json]` | the effective level and each class with its mode and source; `--json` prints principal, project, agent, level, classes, environments, workers |
| `evo-agents hub autonomy set --level A2 [--class K6=deny]... [--reset]` | your own preference; class overrides merge onto those before, `--reset` drops them (A5 unless `--level`) |
| `evo-agents hub autonomy ceiling set --unit <slug> --level A3 --note <text> [--class Kn=mode]... [--environment E]... [--worker W]...` | the next ceiling revision, whole: what it leaves out it drops |
| `evo-agents worker backup --target WHAT --location WHERE [--made-at ISO] [--restore COMMAND]` | inside a run: records a backup (floor S2); the run's log, the audit row `autonomy.backup` and the run's `backups.jsonl` |

A person sees their own autonomy, a lead those of the people in the units they lead, a hub admin everyone's; anyone
else gets 403. Every write adds an audit row: `autonomy.pref_set` for a preference, `autonomy.ceiling_set` for a
ceiling.

## Routes and schema

`GET /v1/autonomy?login=&project=&agent=&repo=` answers the effective autonomy, `PUT /v1/autonomy/prefs` writes the
caller's preference, and `PUT /v1/org/ceilings` takes `classes`, `environments` and `workers`. Schema 0105 adds the
table `autonomy_prefs` (one row per revision of a person's preference) and the columns `overrides`, `environments` and
`workers` of `autonomy_ceilings`. Schema 0106 adds `principal_id` and `autonomy` to `runs`. Schema 0107 adds `secrets.environment`, the decision category
`autonomy` and the notice kind `autonomy`.

The worker's routes, with the token of the worker that claimed the run: `POST /v1/worker/runs/{id}/backups` records a
backup (201: `seq`, `made_at`, `fresh`; 422 for a time in the future), and `POST /v1/worker/runs/{id}/autonomy-acts`
takes a class, the command and a decision's id, and answers the mode now, whether the principal was told, and whether
the decision is answered.
