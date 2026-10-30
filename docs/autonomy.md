# Autonomy levels

The hub sorts what a run may do into twelve classes of action, gives each class a mode, and bundles the modes into
six levels, A0 to A5. A person picks the level they prefer; the org chart's ceilings (`docs/org-chart.md`), the
project policy, the agent profile's seat and a shared credential bound it. The hub holds the classes at its gates
(the lease, the merge, the deploy, the routes), the worker's hook catches the plain commands early, the card tells the
agent where it stands, and five floors and a stop hold whatever the level. The catalog is
`evo_agents.hub.autonomy_levels`, standard library only.

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
6. the credentials shared with the run (`credential_share`): the lowest `level` among the credential shares
   (`docs/credentials.md`) of the run's live leases, and of the share a lease is about to lend from; a share without a
   level limits nothing (`run_autonomy.share_levels`);
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

Source 6 joins at the lease: a run is queued before it holds any lease, so its snapshot starts without the share, and
the lease that lends a shared credential rechecks the run with that share's level, which only ever lowers it. Every
lease records the run's principal (`credential_leases.principal_id`), the dispatcher for a run queued before schema
0113, and a credential share is matched against that principal.

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
labels an env secret (schema 0114 adds `secrets.environment`).

**K5 at the merge** (`evo_agents/hub/server/autonomy_gates.py`): the GitLab merge queue (`merge_gate.ready`) and the
Curator's tier merge (`changes`). A repo on github.com whose project policy has `merge_check: on` adds a step
before a run merges its own pull request: the hub's Judges check it at its head, on the head merged with the base, and
the run waits parked for the verdict (`docs/merge-queue.md`, The merge check on GitHub). The hook of the worker holds
the agent's own merge command to it (below, K5 and the merge check), and the job `hub.merge_queue` reads each merge
afterwards (`evo_agents/hub/server/merge_ancestry.py`). **K6 at the deploy and the release**: the PROD deploy of the policy (`post_merge`) and
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
`kubectl delete`, the recursive deletion of a bucket), K5 (`gh pr merge`, `gh api` of a pull request's merge, `glab mr merge`, a push to a default branch),
K6 (a push of a tag `v*`, `gh release create`, `glab release create`), K8 (`evo-agents hub secret` and `hub account`
writes), K10 (`gh issue|pr create` or `glab issue|mr create` on a repo not the run's) and K12 (`evo-agents hub
admin|policy|agent|org|autonomy` writes).

| Mode | What the hook does |
|---|---|
| deny | refuses, naming the class, the mode and the source |
| ask | refuses, telling the agent to run `evo-agents worker ask --category C` and, once answered, the command again as `EVO_DECISION=<id> COMMAND`; the hub lets it through only for an answered decision of the run whose category is the class's |
| notify | asks the hub (`POST /v1/worker/runs/{id}/autonomy-acts`), which sends the principal the notice `autonomy`, then lets the command run and writes an event of the run |
| auto | lets the command run and writes an event of the run |

**K5 and the merge check.** In a repo with `merge_check: on` the hook adds one more gate to a merge command, in every
mode that lets it run: `gh pr merge` and `gh api` of `repos/OWNER/NAME/pulls/N/merge`. The hook reads the pull request's
head with `gh pr view` and asks the hub (`POST /v1/worker/runs/{id}/merge-gate`). The hub refuses the command unless its
merge check of that pull request has passed at that same head; the refusal says why and names
`evo-agents worker merge-check REPO`, the command that asks. When it has passed, the command must pin the head the hub
judged: `--match-head-commit HEAD` for `gh pr merge`, `-f sha=HEAD` for the API call, so GitHub itself refuses the
merge if a push landed after the check. The order is check, wait for the verdict, then merge. A repo with the check
off, a command that merges no pull request (`glab mr merge`, a push) and a hub that does not answer in 10 seconds leave
the command as its mode said. The hook is a second layer: a merge written some other way gets past it, and the hub
reads the result.

After the merge, the job `hub.merge_queue` looks the pull request up on GitHub for each passed check (up to 14 days
after the check was made), records the merge in the check (`verdict.merged`: the merge commit, the head GitHub merged,
the head judged and whether GitHub's compare finds the merge commit on the base), and opens a notice of category
`autonomy` for the project owner when the merge was at another head than the one judged or the merge commit is not on
the base branch. A clean merge is recorded and tells nobody.

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

## The file floor on the worker

Every run of Claude Code on the worker gets a second `PreToolUse` hook, the file floor (`Floor` in
`evo_agents/worker/autonomy_hook.py`). It is a floor like S2 and S3: it does not follow the level, and a run without a
snapshot gets it too.

- **No write outside the run.** Edit, Write, MultiEdit and NotebookEdit write only into the run's worktree (with the
  repos of a multi-repo plan under it), the system's temporary directories (`/tmp`, `/var/tmp`, `TMPDIR`) and the run's
  own Claude configuration (`CLAUDE_CONFIG_DIR`, else the memory directories under `~/.claude/projects`). The refusal
  names the directories the agent may write in.
- **No read of a secret file.** Read, Grep and Glob, and the Bash commands that read or copy a file (`cat`, `head`,
  `cp`, `scp`, `base64`, `grep` and the other programs of `READERS`, or a `<` redirection), are refused on `.env` and
  `.env.*` (not `.env.example`, `.env.sample`, `.env.template`), SSH keys `id_*`, `*.pem`, `*.key`, `.npmrc`,
  `.pypirc`, `.netrc`, `~/.aws/credentials`, `~/.omelet.json` and the credentials under `~/.omelet.d`, and the token
  files under `~/.evo`. A Glob or Grep pattern that asks for such files (`.env*`, `**/*.pem`) is refused too. A
  program the agent runs that reads `.env` itself (a test, `docker compose`) is left alone.

The floor resolves every path to the real one (relative to the worktree, `~` expanded, symlinks and `..` followed)
before it compares it. Each refusal is a `system` event of the run whose `floor` body names the rule (`write_outside`
or `secret_file`), the tool and the path. A check that fails refuses the call.

| Runtime | The file floor |
|---|---|
| Claude Code | yes, the hook above |
| opencode | not yet: the worker gives the session permission rules for tools, skills and MCP servers, none for paths |
| Codex | not yet: it runs with `danger-full-access` |
| ACP | not yet: the worker grants every permission it is asked |

Like the hook of autonomy, the floor reads what a tool is given: a script the agent writes and runs gets past it.

## The card and the routing of decisions

The hub builds the run's card from its snapshot when a worker claims it (`evo_agents/hub/autonomy_card.py`), and the
prompt of every run with an agent opens with it: the principal, their role and unit, the person they report to when
they are an intern, the effective level, the classes in deny, those in ask with the category to ask each in, and those
in notify. A run at A5 gets the floors S2 and S3 alone. Each class that takes ask has one category of decision: K5
`merge`, K6 `deploy`, K8 `credential`, K9 `delete_data` and `live_migration`, K10 `external_send`, K11 `spend_money`,
K12 `access`; `architecture` and `scope` belong to no class. A plan run's prompt says that merging, deploying and
releasing are the agent's only when K5 and K6 are auto. A run without a snapshot gets the fixed list of categories, as
before.

What the card leaves to the agent, the hub does not ask a person (plan hub-attention-runtime, a3): the ask route
answers 409 to a plan run or an author run asking category `merge` while K5 is auto, `deploy` while K6 is auto, or a
`scope` question asking leave to merge into a default branch, deploy or release while that class is auto, and the
reason quotes the card's rule for the class (`autonomy_card.left_to_agent`). An ask of a project policy shows as auto
on the card, so it is refused too: the merge queue and the release hold it. A K5 that asks on a risk path keeps
`merge`. This is a fixed rule, read from the run's snapshot; no model is asked. A run without a snapshot asks as before
(`docs/notifications.md`, Decisions).

A decision goes to one person (`decisions.asked_of`): for a run whose principal is an intern, the person their seat
reports to, and the leads of their unit too once it has been open 24 hours; for a run the hub makes on its own, the
sponsor of its agent; for any other run, its principal. An intern never answers a decision of their own run (403). A
notice of a class in notify goes to the principal, and to the person they report to when they are an intern. The
Inbox, Telegram and the decision board name the person asked (`docs/notifications.md`).

**A merge approval holds for one head and one scope** (plan super-board-parity-2, a3; `evo_agents/hub/approval_heads.py`).
A yes to a merge is a yes to the code the person saw. The decision keeps what it approves in `decisions.approval`:
the pull request, its head (the commit) and its scope, a hash of the files it touches, the migrations among them and
the lines of its description that say needs-you. Two kinds of decision carry one:

- a decision of category `merge` that an agent asks with `evo-agents worker ask --category merge --pull N`: the worker
  reads the head, the files and the description with `gh pr view` in the run's directory. When the hook later sees
  `EVO_DECISION=<id> gh pr merge N`, it reads the pull request again and sends its head and scope with the act. The
  hub lets the merge through only at the same head and scope; otherwise it answers why (`stale`), and the hook tells
  the agent to ask again at the new head;
- a decision the hub's gate opens for a merge (the key `merge owner/name#N` of the Curator's tier merge, with head
  and scope, or `merge repo!N` of the GitLab queue, with the head alone). When the head or the scope moves, the gate
  cancels the open question of the old head, or leaves the old answer behind, and asks again at the new head.

The question shows what the answer is bound to (`This answer holds for head ... alone`). A bound approval never
opens the gate for another head, nor when the head of the act could not be read. A decision without one, such as a
decision asked before schema 0184 or one that named no pull request, opens the gate as it did.

## The floors S1 to S5

The floors hold for every principal, at A5 too, and no mode lifts them.

| Floor | What holds |
|---|---|
| S1, the ledger | every decision of a gate of the hub writes the audit row `autonomy.decided` with the run, its principal, its agent, the class, the mode and the result (`server/autonomy_ledger.py`); the hook writes its own as run events with the same fields. A run's commits carry the trailers `Evo-Principal: <login>` and `Evo-Agent: <handle>`, and a pull or merge request a run or the hub opens names both, never a runtime or a model |
| S2, the backup | a command of K9 runs only after a backup made within 24 hours (`evo-agents worker backup`, above) |
| S3, no leaked lease | the worker refuses a push whose new commits hold a lease's value, and the hub masks the values of the run's leases in the events, transcripts, outputs and chat messages it stores (`server/run_events.py`, `server/agent_account_mask.py`) |
| S4, the stop | below |
| S5, no borrowed level | a run never acts above its principal: an agent type's run_as bounds it and never raises it, a run one agent asks of another stays within the asking run's snapshot, a shared worker gives the run its principal's autonomy and not its owner's, and a shared credential (source 6) only lowers it |

## The stop S4

`evo-agents hub stop (--login L | --unit SLUG | --all) [--reason TEXT]` stops a person, everyone with a live seat in
a unit or a unit below it, or the whole hub. A person stops their own runs, a lead those of the units they lead and
below, a hub admin anyone and the hub; the agent of a run never does. The stop cancels every run of those principals
that has not ended, as `hub run cancel` does (a held run stops at its worker's next heartbeat), takes back their leases,
and holds every new run of theirs until it is lifted: a dispatch, the Loop, the Curator's night shift and a release get
403 naming the stop (`server/autonomy_hold.py`). `hub stop ... --lift` lifts it, for the same people. Each writes the
audit row `autonomy.stop` or `autonomy.stop.lift` and sends the people stopped the notice `autonomy`. A second stop of
the same scope while one holds is 409. A local run is only recorded by the hub, which neither cancels nor holds it.
The web's `/org` page has Stop and Lift buttons, with a confirmation that counts the runs a stop would cancel.

## Commands

| Command | What it does |
|---|---|
| `evo-agents hub autonomy show [--login L] [--agent H --project P] [--repo R] [--json]` | the effective level and each class with its mode and source; `--json` prints principal, project, agent, level, classes, environments, workers |
| `evo-agents hub autonomy set --level A2 [--class K6=deny]... [--reset]` | your own preference; class overrides merge onto those before, `--reset` drops them (A5 unless `--level`) |
| `evo-agents hub autonomy ceiling set --unit <slug> --level A3 --note <text> [--class Kn=mode]... [--environment E]... [--worker W]...` | the next ceiling revision, whole: what it leaves out it drops |
| `evo-agents hub stop (--login L \| --unit SLUG \| --all) [--reason TEXT] [--lift] [--json]` | the stop S4 of a scope, or `--lift` to lift it |
| `evo-agents worker ask --category merge --pull N [...]` | inside a run: asks a merge decision bound to the head and scope of pull request N, which `gh` reads in the run's directory |
| `evo-agents worker backup --target WHAT --location WHERE [--made-at ISO] [--restore COMMAND]` | inside a run: records a backup (floor S2); the run's log, the audit row `autonomy.backup` and the run's `backups.jsonl` |

A person sees their own autonomy, a lead those of the people in the units they lead, a hub admin everyone's; anyone
else gets 403. Every write adds an audit row: `autonomy.pref_set` for a preference, `autonomy.ceiling_set` for a
ceiling.

## Routes and schema

`GET /v1/autonomy?login=&project=&agent=&repo=` answers the effective autonomy, `PUT /v1/autonomy/prefs` writes the
caller's preference, and `PUT /v1/org/ceilings` takes `classes`, `environments` and `workers`. Schema 0112 adds the
table `autonomy_prefs` (one row per revision of a person's preference) and the columns `overrides`, `environments` and
`workers` of `autonomy_ceilings`. Schema 0113 adds `principal_id` and `autonomy` to `runs`. Schema 0114 adds
`secrets.environment`, the decision category `autonomy` and the notice kind `autonomy`. Schema 0115 adds the decision
categories `merge`, `credential` and `access`, and `decisions.asked_of`. Schema 0116 adds `autonomy_stops`, every stop
with who lifted it and when, never deleted. `POST /v1/autonomy/stops` and `POST /v1/autonomy/stops/lift` stop and
lift, and `GET /v1/autonomy/stops/preview` answers what a stop would cancel, writing nothing.

The worker's routes, with the token of the worker that claimed the run: `POST /v1/worker/runs/{id}/backups` records a
backup (201: `seq`, `made_at`, `fresh`; 422 for a time in the future), and `POST /v1/worker/runs/{id}/autonomy-acts`
takes a class, the command, a decision's id and, for a merge, the `head` and `scope` of its pull request, and answers
the mode now, whether the principal was told, whether the decision is answered and, when its approval was given for
another head or scope, why (`stale`). Schema 0184 adds `decisions.approval`, `{"pull", "head", "scope"}`, which the
ask route takes for a decision of category `merge` alone and the decision view shows.
