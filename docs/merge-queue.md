# The merge queue on GitLab

A project on the hub can let the hub merge its merge requests on GitLab, GitLab Free included, under an autonomy
policy its admins write. Each merge request in scope passes a row of gates before merge, waits for its turn on its
target branch, is rebased, tested again and merged with the exact sha its pipeline passed on, one at a time. After the
merge the hub follows the branch's UAT check, releases PROD as the policy says, reverts the merge request that broke a
check or rolls PROD back, and stops a repo with its breaker when too much goes wrong. People write the policy, reset a
breaker and answer audit samples; they do not review each merge request.

A project with no policy, or a repo the policy leaves `off`, works as before: merge requests on GitLab stay open for a
person, and the Curator merges on GitHub by its tiers alone (`docs/curator.md`). No merge train or merged results
pipeline of GitLab Premium is used.

| Part | Where |
| --- | --- |
| the policy's keys, defaults and checks | `evo_agents/hub/autonomy.py` |
| the policy's revisions and routes | `evo_agents/hub/server/autonomy_policies.py`, `evo_agents/hub/cli_policy.py` |
| the forge secret and the GitLab client | `evo_agents/hub/server/forge.py`, `evo_agents/hub/server/gitlab.py` |
| scope, gates, the Judges' questions, the risk | `evo_agents/hub/merge_gate.py`, `evo_agents/hub/server/merge_gate.py`, `evo_agents/hub/server/merge_judge.py` |
| the turn: rebase, pipeline, merge | `evo_agents/hub/server/merge_turn.py` |
| after the merge: checks, PROD, revert, rollback, samples, figures | `evo_agents/hub/post_merge.py`, `evo_agents/hub/server/post_merge.py` |
| the breaker | `evo_agents/hub/merge_breaker.py`, `evo_agents/hub/server/merge_breaker.py` |
| an admin's pass of a hold for a person | `evo_agents/hub/server/merge_pass.py` |
| the routes of the queue and the command line | `evo_agents/hub/server/merge_view.py`, `evo_agents/hub/cli_merge.py` |
| the web's Merge queue pages | `web/src/components/merge-queue/`, `web/src/lib/merge-queue.ts`, `web/DESIGN.md` |
| the tables (schema 0079 to 0084) | `autonomy_policies` (`tables_settings.py`); `merge_queue`, `merge_events`, `merge_samples`, `merge_breakers` (`tables_merge.py`) |

Everything runs in the job `hub.merge_queue` of the hub's worker, every minute, one pass at a time on all the hub's job
workers (`docs/hub.md`, Worker and queue).

## Setting a repo up

1. **A forge secret.** An admin of the project writes a GitLab project or group access token (scope `api`, role
   Maintainer) as a secret of kind `forge` whose `url_prefix` covers the repo's origin (Forge secrets, below).
2. **The GitLab project's settings** (gate 0): merge method fast-forward, "Pipelines must succeed" on, "Skipped
   pipelines are considered successful" off, and the target branch protected with push allowed to no one and merge to
   Maintainers. A missing setting keeps every merge request of the repo waiting, and the hub comments once on each with
   what is missing.
3. **The CI jobs** the policy names, all optional: the UAT check, the PROD deploy, the PROD check and the rollback. The
   policy names them; writing them is the repo's own work. A job the policy names that is missing from a pipeline counts
   as a failed check. The UAT deploy and check of one commit must not interleave with the next commit's; a repo that
   cannot promise it sets `merge.hold_until: uat_check`.
4. **A worker that judges.** The Judge runs on `judge.worker`, else on the worker on duty of the project's charter. Its
   daemon must say it judges merge requests (the feature `merge_judge` of its heartbeat).
5. **The policy**, with the repo in mode `auto`: `evo-agents hub policy set --file policy.yaml`.

```yaml
# policy.yaml
repos:
  lumen-api:
    mode: auto
    prod_release: after_uat
    after_uat_hours: 4
    jobs:
      uat_check: smoke-uat
      prod_deploy: deploy-prod
      prod_check: smoke-prod
      rollback: rollback-prod
merge:
  hold_until: merged
audit:
  sample_rate: 0.1
```

## The autonomy policy

A policy is an object of sections. A revision holds only the keys its writer set; a key it leaves out reads as its
default, so a key a later release adds reads its default in every older revision. A project that never wrote one is at
revision 0: every repo `off`.

| Key | Default | What it says |
| --- | --- | --- |
| `repos.<repo>.mode` | `off` | `auto`: the hub merges the repo's merge requests in scope; `off`: a person does. `auto` is refused for a repo on github.com |
| `repos.<repo>.target_branch` | null | the branch the queue merges into; null: the repo's default branch on the hub |
| `repos.<repo>.prod_release` | `immediate` | when the hub runs the PROD deploy: `immediate`, `after_uat`, or `manual` |
| `repos.<repo>.after_uat_hours` | 4 | hours of a green UAT check before the PROD deploy, with `after_uat` |
| `repos.<repo>.jobs.uat_check`, `.prod_deploy`, `.prod_check`, `.rollback` | null | the CI job of each; null: the repo has none |
| `merge.opt_in_label` | `evo-merge` | the label a person puts on a merge request to ask for the queue |
| `merge.risk_paths` | `[]` | globs added to the risk paths the code knows |
| `merge.hold_until` | `merged` | how far the merge request before must get before the next one of the same branch starts: `merged`, `uat_check`, `prod_check` |
| `merge.pipeline_minutes` | 60 | the longest the hub waits for a pipeline, a rebase or a job |
| `judge.worker` | null | the worker that runs the Judge; null: the charter's worker on duty |
| `judge.model` | null | the first Judge's model; null: a Claude model other than the Builder's |
| `breaker.target_red_minutes` | 30 | the target branch red this long trips the breaker |
| `breaker.reverts_in_a_row` | 2 | this many automatic reverts in a row trip it |
| `breaker.revert_rate_factor` | 2 | a 14-day revert rate this many times the repo's baseline trips it |
| `breaker.revert_rate_max` | 0.10 | the revert rate that trips it while the repo has no baseline |
| `breaker.baseline_min_merges` | 10 | merges of the first 14 days a baseline needs |
| `breaker.judge_misses_7d` | 2 | passes of the Judge found wrong in 7 days that it allows |
| `audit.sample_rate` | 0.10 | the share of merged merge requests an admin is asked to check |

The policy has no key that caps the size of a diff or the merges of a day: no merge request waits for its number of
lines or files, and the breaker never counts merges per day.

Every member with a grant on the project reads the policy; only an admin of the project writes it. A write is the whole
policy, never a patch, and becomes the next revision with the writer's note; a write equal to the newest revision
writes nothing. `--if-revision` (`if_revision` of `PUT`) refuses a write when someone wrote another revision meanwhile
(409). Writing is strict: an unknown key, a bad value, a repo the project does not list, or `auto` on github.com is 422.
Reading is lenient: a value a newer release refused reads as its default. Every revision is kept and adds an audit row
`policy.set` naming the revision and the keys it changed, never a value.

An agent never writes the policy. @builder proposes a change as a proposal of kind `policy` that names each key by its
dotted path; an admin of the project approves it in the Inbox and the hub writes it as the next revision, with the note
`proposal N of @builder: ...`. Rolling the proposal back writes a copy of the revision before it
(`docs/plugins.md`, Builder).

```sh
evo-agents hub policy show --project demo                  # every key now, (default) where the policy sets none
evo-agents hub policy show --revision 3 --project demo
evo-agents hub policy set --file policy.yaml --note "lumen-api on auto" --if-revision 3 --project demo
evo-agents hub policy history --project demo               # the revisions, newest first, with the keys each changed
evo-agents hub policy diff 2 4 --project demo              # the keys whose value differs (0 is no policy)
```

In `policy.yaml` only `true` and `false` are booleans, so `mode: off` stays the word `off`.

## Which merge requests

A merge request is in scope when its repo is in mode `auto`, a forge secret of the project covers the repo's origin,
it targets the repo's target branch, and it comes from a branch of the same GitLab project (never a fork). It comes
from one of:

- `plan`: the hub opens it with the forge secret once every step a plan names for the repo is done, plan runs and step
  runs alike, from the branch the plan names for the repo; or it takes the merge request already open on that branch;
- `task`: the hub opens it from a task run's branch once the run ended done;
- `builder`: the Curator's, which a Builder's push opened and whose Judge passed it (`docs/curator.md`);
- `label`: a person's, carrying `merge.opt_in_label`, put there by a member of the GitLab project with the role
  Developer or above;
- `revert`: the revert the hub opens of a merge that broke a check (After the merge, below).

A draft waits. Anything else stays open for a person, untouched. A merge request leaves the queue when its repo goes
back to `off`, when it goes out of scope, or when a person merges or closes it on GitLab.

## The gates

Each merge request passes these in order, the cheap ones first. Each gate is a line of the queue's ledger with its
reason, and the first that does not pass stops the others. A gate that waits for something on GitLab (a pipeline, a
setting, the end of a draft) keeps the merge request `gating` with why; any other failure makes it `blocked`, which only
a new push moves. The hub comments on the merge request once for each reason that blocks it.

| Gate | Passes when |
| --- | --- |
| 0, the repo | the GitLab project's settings of Setting a repo up, step 2, hold |
| 1, the pipeline | the head pipeline ran on the merge request's head and succeeded; a skipped pipeline, or a head commit that asks CI to skip it, is not green; a running one waits up to `merge.pipeline_minutes` |
| 2, the plan's repo | the merge request is of the repo's GitLab project and from the branch the plan, task run or Curator's change names; a person's passes |
| 3, score hacking | the diff shows no sign of score hacking (`judge.hack_signs`); one sign holds the merge request for a person |
| 4, the checks | the judge run runs the verify of the plan's steps for the repo and the project's hidden checks before its agent reads the diff; each exits 0 |
| 5, the Judge | Claude, in a context of its own that never holds the Builder's transcript, passes each of `work`, `description` (the title and description match the diff), `tests`, `scope` and `safety` |
| 6, the risk | computed, not judged: high when a changed path matches the risk paths (migrations, SQL, auth, security, permissions, deploy files, Docker, Kubernetes, Helm, Terraform, `.env`, secrets, dependency manifests and lockfiles) or `merge.risk_paths`, when the path rules of the Curator's tiers give it level 2 or more, or when the knowledge graph says the diff reaches such a path; a graph that cannot tell is named in the reasons, and the risk then comes from the paths alone |
| 7, the second Judge | for a high risk alone: Opus, on `migration`, `auth`, `deploy`, `dependencies` and `rollback` |

**A hold for a person.** Gate 3 is the one gate that holds a merge request for a person: the signs of score hacking
may be the work the plan asked for, such as a plan run that adds its tests to `.gitlab-ci.yml`. An admin of the project
reads the diff and passes the hold with a reason (`evo-agents hub merge pass REPO IID --reason ...`, or `POST
.../merge-queue/{id}/pass`). The merge request is gated again at the same head and the queue goes on from that gate:
the risk, the checks, the Judges (unless their verdict holds for the patch), its turn, the pipeline at its rebased head
and the merge all still apply, and at the rebased head gate 3 lets the signs through while the patch is the one the
gates passed. A new push, or a rebase that changes the patch, takes the pass back, and the hold it comes to again needs
a new pass. The ledger has the pass as a line of gate 3 passed that names the admin and the reason, and the gate's
line at the next pass names the pass; the audit says `merge.pass`. Any other block moves only with a new push, and a
revert's gate 3 (its diff undoing the merge) is passed by no person. The hub's comment on a merge request held at gate
3 says that an admin may pass it, and the decision board's item of a held merge request says how it goes on.

A judge run of the queue is a run of kind `judge` on no plan and no schedule, dispatched as the admin who wrote the
policy's newest revision. A project has one judge run active at a time, the Curator's included. A judge run that ends
without a verdict is queued again, three times in all, then the merge request is blocked. A verdict holds for a patch,
not a commit: the lines each file adds and removes, without line numbers or context. A rebase that keeps the patch keeps
the verdict, and the Judge does not run again.

## The queue

A merge request whose gates passed is `ready`. Each target branch of a repo has one queue, and one merge request of it
has the turn at a time: the one ready first. A revert goes first. A merge request merged before it holds the next turn
back until it reaches `merge.hold_until`: `merged` (the default) holds nothing, `uat_check` and `prod_check` wait for
that check of the merge.

1. **Rebasing.** The hub reads the head of the target branch, asks GitLab to rebase the merge request onto it, and
   waits for GitLab. A rebase GitLab fails, or one still running after `merge.pipeline_minutes`, blocks it. A merge
   request whose head already holds the head of the target branch, a revert opened on it among them, is not rebased:
   GitLab would start another pipeline on the same head and refuse the merge while it runs, though the head pipeline
   it shows for a moment is still the green one before it.
2. **Testing.** The target branch moving sends it back to `ready` at the head of the queue, to be rebased again; after
   three rebases in a row it is blocked. A push of someone else sends it back to its gates. The pipeline of the new head
   that fails runs again once, and failing again blocks the merge request. Green, gates 0, 1 and 3 are read again at
   that head: the same patch goes on to its merge; a new patch goes through its gates and Judges again, keeping its
   place.
3. **Merging.** GitLab merges it only while its head is still the sha its pipeline passed on, by fast-forward. A push
   in between sends it back to its gates; the target branch moving sends it back to `ready`; a pipeline of its head
   still running (`ci_still_running`) sends it back to testing, to wait for that pipeline; any other refusal blocks it
   with GitLab's words.

Each move is written to Postgres before the GitLab call it leads to and read back from GitLab at the next move, so a
hub that restarts mid-turn goes on where it was. Each move is a line of the ledger (`rebased`, `pipeline`, `retried`,
`gate`, `head`, `queued`, `merged`) and each merge an audit row `merge.merge`.

## After the merge

**The checks.** Each merge has its commit on the target branch and the pipeline that commit runs there. The UAT check
is the job `jobs.uat_check` of that pipeline; a repo that names none has the pipeline itself as its check and no check
after deploy. The PROD check is `jobs.prod_check` after `jobs.prod_deploy`. The hub plays a manual job. A job that fails
or is cancelled runs again once to rule a flake out. A check ends `passed`, `failed` (failing again after its retry),
or `none`: cancelled or skipped after its retry, a named job missing from the pipeline, or still running after
`merge.pipeline_minutes`.

**The culprit.** The last good commit of a branch is the newest merge whose check passed; every merge before it is
cleared with it. When a later check fails or has no result, the merges from the oldest after the last good one up to it
are the suspects, and the oldest decides: its own check failed, and it is the culprit; its check still runs, and the
hub waits; its check has no result, and the hub reverts nothing and asks each admin of the project in the Inbox,
listing the suspects. An admin then asks for the revert of one.

**The revert.** The hub opens `evo-revert/<iid>` from the target branch with a revert of each commit of the culprit,
newest first, and its merge request goes first in the queue. It passes the pipeline and a check that its diff undoes the
merged diff exactly, never a Judge or a sample. Once it merges, the culprit is `reverted`.

**PROD.** Deploys of a branch go in merge order, one at a time; the next starts once the PROD check of the one before
ended.

| `prod_release` | The hub runs the PROD deploy |
| --- | --- |
| `immediate` | of the oldest merge whose UAT check passed, at once |
| `after_uat` | of the newest merge whose UAT check passed `after_uat_hours` ago |
| `manual` | of the merge an admin approved: each admin gets the question in the Inbox, and Approve release on the Merge queue page answers it |

A merge that comes after a culprit whose revert is not merged yet is never deployed alone: the revert's deploy carries
it. A PROD check that fails after its retry, or has no result, has the hub run `jobs.rollback` first, with the variable
`EVO_ROLLBACK_TO_SHA` set to the last commit whose PROD check passed. The deploy's merge is then the culprit, reverted
as above, only when the deploy carried no other merge; otherwise the hub rolls back and reverts nothing. A rollback
always trips the breaker.

The Inbox notices of what follows a merge are of the kind `merge_default_branch`, name the repo, the branch and the
commits, carry what the hub asks (`sample`, `release`, `revert`) and link to the project's Merge queue page. Each step
is a line of the ledger (`uat_check`, `prod_deploy`, `prod_check`, `retried`, `rollback`, `revert_opened`, `reverted`,
`sampled`, `sample_answered`) and what acts on GitLab an audit row (`merge.deploy`, `merge.rollback`, `merge.revert`).

## The breaker

Each repo has a breaker, armed until one of these trips it, the thresholds being the policy's `breaker.*` keys:

| Cause | Trips when |
| --- | --- |
| `uat_check`, `prod_check` | a UAT check, PROD deploy or PROD check failed again after its retry, or has no result |
| `rollback` | the hub rolled PROD back |
| `revert` | GitLab would not revert a culprit (a conflict), or the revert's merge request was blocked |
| `target_red` | the newest pipeline of the target branch failed, whoever pushed it, and none passed since, for more than `breaker.target_red_minutes` |
| `reverts_in_a_row` | `breaker.reverts_in_a_row` merges in a row were found the culprit of a failed check |
| `revert_rate` | the share of the merges of the last 14 days found the culprit is over `breaker.revert_rate_factor` times the repo's baseline (its first 14 days in mode `auto`); before a baseline of `breaker.baseline_min_merges` merges, over `breaker.revert_rate_max` |
| `judge_misses` | more than `breaker.judge_misses_7d` merges the Judge passed were found wrong in 7 days: reverted, rolled back or sampled faulty |

Only what happened since the last reset counts, so a reset is never undone by what came before it. The merges of a day
never trip it.

Tripped, the hub rebases, merges and deploys nothing of that repo: its merge requests `ready` or in their turn go
`paused`, those at their gates wait there, and no PROD deploy starts. What undoes the failure still runs to its end:
the rollback, the PROD check of a deploy already under way, and the reverts, which go through their gates and their
turn. The hub keeps following the checks of the merges already merged, to find the culprit. Each admin of the project
gets a notice `merge_paused` in the Inbox, and on Telegram or any channel they linked. Other repos of the project go on.

Only an admin of the project resets it, from the Merge queue page or with `evo-agents hub merge resume REPO`: the
breaker is armed again, its paused merge requests are ready again at their place, its notices are read, and the audit
says `merge.resume`.

## Audit samples and figures

A merge request the queue merged, a revert aside, is drawn for an audit sample at merge time with the policy's
`audit.sample_rate`. Each admin of the project gets it in the Inbox and answers `ok` or `faulty` with a note, there or
on the Merge queue page. A `faulty` answer counts as a pass of the Judge found wrong, toward `judge_misses`.

The figures count, for each repo, agent profile (`person` for a person's merge request) and model: merges, culprits
reverted, PROD rollbacks, passes of the Judge found wrong, and the time spent in the queue and to the checks
(`GET .../merge-queue/figures?days=N`, and the Figures tab).

## Forge secrets

A secret of kind `forge` is the hub's own: only the hub's job worker opens it, right before it calls GitLab, and no run
is ever leased it or shown it. Writing one needs the admin role on each of its projects, an end date, a `url_prefix`
not on github.com, no username and no worker. When a repo's origin has several, the hub uses the one whose
`url_prefix` covers it the longest, then the one written last, and only while its owner still holds the admin role on
the project. `docs/credentials.md`, Forge secrets, has the details.

```sh
pbpaste | evo-agents hub secret set gitlab-merge --kind forge --url-prefix https://gitlab.example.org/northwind \
  --project demo --expires 2027-09-15
```

## Commands, routes and pages

```sh
evo-agents hub merge list --project demo                 # each repo's breaker, then the merge requests, newest first
evo-agents hub merge list --repo lumen-api --limit 20 --project demo
evo-agents hub merge resume lumen-api --project demo     # an admin resets the repo's breaker
evo-agents hub merge pass lumen-api 11 --reason "its goal is this CI job" --project demo  # an admin passes a hold
```

| Route | Who |
| --- | --- |
| `GET /v1/projects/{project}/policy`, `GET .../policy/revisions`, `GET .../policy/revisions/{revision}`, `GET .../policy/compare?from=N&to=M` | a member with a grant |
| `PUT /v1/projects/{project}/policy` | an admin of the project |
| `GET /v1/projects/{project}/merge-queue` (`?repo=`, `?limit=`), `GET .../merge-queue/figures` (`?repo=`, `?days=`) | a member with a grant |
| `POST .../merge-queue/samples/{id}`, `POST .../merge-queue/{id}/release`, `POST .../merge-queue/{id}/revert`, `POST .../merge-queue/{id}/pass` (`{"reason": ...}`), `POST .../merge-queue/repos/{repo}/resume` | an admin of the project |
| `GET /v1/worker/runs/{id}/merge-judge`, `POST /v1/worker/runs/{id}/merge-verdict` | the worker holding the judge run, with the run's key |

The web's Merge queue entry of a project, after Runs, has three tabs: Queue (`/p/{project}/merge-queue`: the merge
requests waiting, in their turn and after their merge, why each waits or is blocked, the events after a merge, each
repo's breaker, and the samples waiting for an answer), Figures (`.../merge-queue/figures`) and Policy
(`.../merge-queue/policy`: the policy now, its form, its revisions and the comparison of two). A reader sees them all;
an admin also edits the policy, answers samples, approves a manual release, asks for a revert and resets a breaker,
the last two behind a confirmation. The page shows a hold at gate 3 and why; passing it is the command's and the
route's for now.
