# The daily release

A project on the hub can have a daily release. Once a day, at a local time its admins set (06:00 in Asia/Ho_Chi_Minh by
default), the hub checks without any model whether the code that makes the package changed between the newest release
tag and the commit of the tag `nightly`. When it did, an agent on a worker the hub picks cuts the next version on a
branch, and the hub merges that branch and tags the merge commit. The hub then follows the tag's `release.yml` until its
job `publish` puts the version on PyPI; its job `github-release` then makes the tag's GitHub Release with notes GitHub
generates from the merged pull requests, and the same wheel and sdist. A red CI is fixed forward with the next patch, as
often as it takes, with no cost cap. The cycle stops and asks its owner what next when the same test fails at two tags
in a row, a run fails, the CI overruns, or a fix shows signs of test hacking.

No GitHub Actions cron and no model credential in GitHub's secrets are involved: the hub's job runner makes the check,
a member's worker runs the agent, and the hub's GitHub App, never the agent, merges and tags.

| Part | Where |
| --- | --- |
| the schedule, the check, the cycle | `evo_agents/hub/releases.py`, `evo_agents/hub/server/release_check.py`, `evo_agents/hub/server/release_github.py` |
| the range, the version rule, the prompts, the refusals | `evo_agents/hub/release_runs.py`, `evo_agents/hub/server/release_claims.py` |
| landing: pull request, merge, tag | `evo_agents/hub/server/release_land.py` |
| following the CI, failures, infrastructure, stop causes | `evo_agents/hub/release_ci.py`, `evo_agents/hub/server/release_watch.py`, `evo_agents/hub/server/release_actions.py` |
| stopping, the owner's decision and its answer | `evo_agents/hub/server/release_cycles.py`, `evo_agents/hub/server/release_answer.py` |
| the routes and the command line | `evo_agents/hub/server/releases.py`, `evo_agents/hub/release_cli.py` |
| the run on the machine | `evo_agents/worker/runner/release.py`, `evo_agents/worker/gitops.py` (`check_push`) |
| the web's Releases page | `web/src/components/releases/`, `web/DESIGN.md` |
| the tables (schema 0067) | `evo_agents/hub/tables_release.py`: `release_schedules`, `release_cycles`, `release_runs`, `release_tags`, `release_checks` |

## One cycle

1. **The check.** Each minute the job `hub.release_schedules` of the hub's worker looks for schedules whose local time
   came. The day's check reads the newest tag the schedule's pattern matches and the commit of its target ref, and
   compares the two trees under the packaging paths. A change opens a cycle and queues its first run, of kind
   `release` in mode `cut`.
2. **The cut.** A worker of the schedule's owner, or one shared with them, claims the run. Its agent picks the version by the rule below, writes
   one `Decision:` line, runs `scripts/bump_version.py`, looks in the range for three faults that fail only on macOS
   and Windows, and commits. The daemon pushes the branch `release/<version>`, and nothing else.
3. **The landing.** The hub opens the pull request of that branch into the default branch, checks it, merges it with a
   merge commit at the commit the run pushed, and tags that merge commit `v<version>`, all with its GitHub App.
4. **The CI.** The hub follows the run of `release.yml` for the tag through the Actions API.
5. **Published.** The workflow succeeded, its jobs `publish` and `github-release` included: the cycle is `published`,
   and its owner gets a notice of kind `release`, on the web and on their linked channels such as Telegram, with the
   version, the workflow run and `https://pypi.org/project/evo-ak/<version>/`.
6. **Red.** The hub reads the failed jobs and the end of their logs. A fault of the infrastructure alone gets the
   failed jobs run again once. Anything else queues a run in mode `fix`, which cuts the next patch with the fix on
   `release/<patch>`; the hub lands and tags it as in 3, and follows the new tag. The red tag stays as it is.
7. **Stopped.** On a stop cause (below) the cycle stops, the hub posts a thread in the project's triage chat and asks
   the owner a decision: go on, close the cycle, or pause the schedule.

## The schedule

A project has at most one release schedule. It is apart from the Curator: the night shift's schedules, charter, pause,
resume and circuit breaker never touch it, and pausing the release never touches the night shift.

| Key | Default | What it is |
| --- | --- | --- |
| `repo` | the project's only repo on GitHub | the repo to release, one of the project's |
| `at`, `timezone` | `06:00`, `Asia/Ho_Chi_Minh` | the local time of the daily check; the zone must be one Postgres knows |
| `target_ref` | `nightly` | the ref whose commit a release ships: a tag, then a branch, of that name |
| `tag_pattern` | `v*` | the release tags: a literal prefix, then `*`; the newest is the highest `X.Y.Z` after the prefix, pre-releases ignored |
| `workflow` | `release.yml` | the workflow file that tests and publishes a tag |
| `paths` (`--path`) | `evo_agents/`, `plugins/`, `web/`, `.claude-plugin/`, `pyproject.toml`, `uv.lock` | the packaging paths, 1 to 50; an entry ending in `/` is a directory |

Only an admin of the project sets it, and the admin who set it last is its **owner**: the runs of its cycles are
dispatched as that member, through `dispatched_via` `release`, and pinned to no worker. Any worker of the owner, or one
shared with them, takes them when it has a checkout of the repo and its daemon says it runs release runs; a worker that
takes only runs dispatched from the web passes them over. A project has at most one release run active at a time. The
check records the outcome `owner`, and the hub lands nothing, once the owner no longer holds the admin role on the
project.

## The daily check

The job runs every minute, one pass at a time. A schedule whose local time came in the last 60 minutes and has no
check of that local day yet gets one, so a hub that was down at 06:00 still checks if it is back before 07:00. The
scheduled check of a day is kept once, under a unique index on the day: a second pass of the job, or a hub that
restarted, finds it and does nothing. A check that failed (outcome `error`) is tried again on the next minutes, 3 times
in all that day.

The check looks at its gates first and reads nothing of GitHub when one closes: the schedule is paused, a cycle of the
schedule is still `open` or `stopped`, or the owner lost the admin role. Otherwise it reads, with the workers' App, the
tags and the target ref (one `ls-refs` of git's HTTP protocol), the CI of the newest tag, and the whole trees of the two
commits. A file whose only change is its executable mode does not count. Then, under the schedule's row lock, it looks
at the gates again and records the check:

| Outcome | When |
| --- | --- |
| `opened` | a packaging path changed, or the newest tag's CI is red: a cycle opened and its first run queued |
| `unchanged` | no packaging path changed since the newest tag, whose CI is not red |
| `busy` | a cycle of the schedule is open or stopped |
| `paused` | the schedule is paused |
| `owner` | the owner no longer holds the admin role on the project |
| `error` | the check could not read what it compares: GitHub did not answer, the App is not installed, no tag matches |

When the newest tag's CI is red (its run of the workflow completed, did not publish, and a job of it failed), whoever
made the tag, the cycle starts with a fix run of that tag even when no packaging path changed. A CI the hub cannot read
counts as not red, and the check's reason says why (`actions_permission: ...`).

When it opens a cycle, the check also reads the range of the release once and keeps it on the cycle (`span`): the
commits from the tag to the target as GitHub compares them (at most 250), the pull requests their merge commits name
("Merge pull request #N from owner/branch", or "(#N)" for a squash), and the migrations the target's tree adds under
`evo_agents/hub/migrations/versions/`. The claim builds the prompt from it without calling GitHub. When the comparison
fails, the cycle still opens, and the prompt tells the agent to read the range with git.

`evo-agents hub release run` makes a check now, through the same gates, recorded as a manual check. A day may hold any
number of them.

## The version rule

The version moves one step from the newest tag's:

- the next minor (`X.Y+1.0`) when the range adds a migration under `evo_agents/hub/migrations/versions/` or merges a
  pull request from a branch `feat/...`;
- the next patch (`X.Y.Z+1`) otherwise, and always for a fix run;
- never a new major.

The prompt names the version the hub's reading suggests and asks the agent to check it against git. The agent writes
one line that starts with `Decision:` and says the version and why; the hub puts that line, with nothing GitHub acts on
(no keyword that closes an issue, no mention), into the pull request's body.

Before it merges, the hub refuses the branch when the version is not `X.Y.Z`, changes the major, is not greater than
the version `evo_agents/__init__.py` names on the default branch, is neither its next patch nor its next minor, has a
tag already, or is not what `evo_agents/__init__.py` names on the branch (the bump did not run). A refusal stops the
cycle with the reason.

## The run on the worker

A release run is a headless Claude Code run that asks nobody anything. Its cap is 120 minutes of agent time, with a
timeout 5 minutes longer and no cost cap, and it is tried once: a run the hub lost fails, and the cycle decides what
comes next.

The daemon starts the worktree at the commit the claim names (a cut: the target's commit; a fix: the red tag's commit)
on a branch of the run, `evo-run/<run>/<folder>`. The prompt of a cut names the range, the version rule, the bump and
three faults to look for in the range's diff:

- a test module that imports SQLAlchemy or psycopg at its top without `pytest.importorskip` before it;
- a file written with `write_text`, or `open` in text mode, without `newline="\n"`, whose bytes a test compares: on
  Windows its lines end in CRLF;
- a `sqlite3` connection that is never closed: Windows keeps its file locked.

A fix's prompt names the red tag, the next patch and what the hub read of the failure, and forbids deleting a test,
skipping one other than with `pytest.importorskip` or a skip on one platform with its reason, and making an assert
always true.

The agent commits on the run's branch and writes `version`, `decision` and `summary` in `.evo-run/result.json`. When it
ends, the daemon commits what it left, pushes HEAD to `release/<version>`, never a default branch, never a tag and
never forced (`gitops.check_push` refuses every other ref for kind `release`), and tells the hub
(`POST /v1/worker/runs/{id}/release/branch`). A result with no version, no commit beyond the start, or a push the hub
does not take fails the run.

The run is guarded as a run of the Curator is: its agent gets no credential helper and no lease that pushes; only the
daemon's own git uses the token the hub leased (`docs/credentials.md`, "Which leases a run gets").

## Landing

The job's pass lands each release run that is done, pushed its branch and belongs to an open cycle. One token of the
workers' App for that repo alone, with contents and pull requests write, serves the whole landing and is revoked after
it. The hub:

1. reads the version on the default branch's tip and on the run's commit, and the release tags, and refuses as above;
2. opens the pull request "Release X.Y.Z" of `release/<version>` into the default branch, or finds the one open;
3. reads its files and never merges a change with a sign of test hacking: a test removed, an assertion removed or
   loosened (an assert made always true among them), or a skip or xfail added, other than `pytest.importorskip` and a
   skip on one platform with `reason=` (the signs of `judge.hack_signs`); such a pull request stays open and the cycle
   stops (cause `hacking`);
4. merges it with a merge commit at the run's commit alone, so GitHub refuses once the branch moved;
5. makes the lightweight tag `v<version>` on the merge commit (`POST .../git/refs`), audited as `release.tag`.

Each part is saved as it is done, so a pass that GitHub failed goes on from there. GitHub failing counts one try; 5
passes in a row mark the run failed and stop the cycle (cause `github`). A refusal stops it at once (cause `refused`).

## Following the CI

The hub reads the runs of the schedule's workflow for the tag's commit, pushed as that tag, then the jobs of the run's
latest attempt. It reads the Actions API rather than check runs, because other workflows (images, deploy) run on the
same commits with jobs of the same names. Only the newest tag a cycle follows is watched.

- **Passed**: the run concluded `success` and its job `publish` succeeded. The cycle is published.
- **Failed**: the hub reads up to 8 failed jobs and the last 64 KiB of each log. It takes the ids of the tests that
  failed (pytest's `FAILED` and `ERROR` node ids, the failed tests of Playwright's summary), else the job's name and
  the step that failed.
- **Infrastructure**: when every failed job's log matches a rule (the runner lost, the network while installing, PyPI
  answering 5xx) and names no test, the hub runs the failed jobs again, once, and waits for the new attempt.
- **Fix**: any other failure queues a fix run with what the hub read, unless a stop cause holds.
- **Timeout**: no run of the workflow 30 minutes after the hub began to follow the tag, or a run going over 3 hours
  from the later of its start and the follow, stops the cycle (cause `timeout`). A run that ended cancelled, or red
  with no failed job, stops it too (cause `ci`).

A tag a person pushes while a cycle is open becomes the tag the cycle follows, when its version is greater than every
tag the cycle follows and its base tag.

## When a cycle stops

| Cause | When |
| --- | --- |
| `run_failed` | a run of the cycle failed |
| `run_cancelled` | a run of the cycle was cancelled while the schedule was not paused |
| `refused` | the hub refused to land a run, or a run ended done without its branch |
| `github` | GitHub failed 5 passes in a row while the hub landed a run |
| `hacking` | a run's change shows a sign of test hacking |
| `budget` | what was left of the cycle's cap was under 0.01 USD; only cycles from before schema 0176, which dropped the cap |
| `same_test` | one test id failed at two tags in a row of the cycle |
| `timeout` | no workflow run in 30 minutes, or one over 3 hours |
| `ci` | the workflow ended in a way no fix answers: cancelled, or red with no failed job |

A stopped cycle moves no further and no check opens another cycle while it is there, until its owner answers or a
newer tag supersedes it (below). The hub posts a thread in the
project's triage chat with the cause, the tags with their workflow runs, the runs and the cost, and asks the owner a
decision of category `release` on the cycle's last run, with three options:

| Option | What the hub does |
| --- | --- |
| `continue` | opens the cycle again and goes on where it stopped: the newest tag's CI followed again from now, a red newest tag gets a fix run, a cycle with no tag yet a new cut run |
| `close` | closes the cycle, its reason kept |
| `pause` | pauses the schedule as `evo-agents hub release pause` does, and closes the cycle |

The decision reaches the owner on their channels. The answer needs the admin role on the project, and comes from the
Inbox on the web or from `evo-agents hub decision answer N --option continue --project evo-agents`; the hub says what it
did in the cycle's thread. The run the decision names has ended by then: the hub, not an agent, takes the answer, so
the Inbox offers the options whatever state that run is in, and an answer must pick one of them. A cycle no longer
stopped takes no answer (409).

A release made outside the cycle supersedes it. While a cycle is stopped, the daily check still reads the newest tag;
when that tag is newer than any the cycle knows (the tag it started from and the tags it made) and no run of the cycle
is active, the check closes the cycle with the tag in its reason, cancels the decision and marks its notification read,
says so in the cycle's thread, and goes on from that tag as if no cycle were there: a red tag opens a fix cycle, a
change of a packaging path a cut. A tag no newer than the cycle's own keeps the check `busy` until the owner answers.

## No cost cap

A cycle has no cost cap, and its runs have none (`max_usd` null): the hub counts what they cost, each agent session of
the cycle once, and shows it, but never stops a cycle on it. The number of tags in a cycle has no limit either: the
stop causes are the only limits. Schema 0176 dropped the cap a schedule (`cycle_budget_usd`, 200 USD by default) and a
cycle (`budget_usd`) used to keep.

## The GitHub App

Everything done on GitHub for a release goes through the workers' App, evo-agents-workers (`EVO_HUB_GITHUB_APP_ID`,
`EVO_HUB_GITHUB_APP_PRIVATE_KEY`). The hub's own calls take a token made for each use, scoped to the one repo and
revoked after it; the run's push takes the lease a run gets (`docs/credentials.md`, "Which leases a run gets"):

| Use | Permissions |
| --- | --- |
| the check: refs, trees, the range | contents read |
| the run's lease, for the daemon's push of `release/<version>` | contents write, metadata read; never workflows write |
| the landing: the pull request opened, its files read, merged, the tag made (one token) | contents write, pull requests write |
| reading the workflow's runs, jobs and logs | actions read |
| running the failed jobs again | actions write |

The App therefore needs Contents (read and write), Pull requests (read and write), Actions (read and write) and
Metadata (read), and the installation on the repo must grant them: after the App asks for a new permission, a member
of the account accepts it on the installation's page. Until the installation grants Actions, the hub cannot read the
CI: the tag and the status say `actions_permission`, the owner gets one notice of kind `release`, and the deadlines
still count. A ruleset that keeps others off the default branch, or that restricts tags matching the pattern, must
let the workers' App merge and create the tag; otherwise GitHub refuses and the cycle stops (cause `refused`).

A release run's token never asks `workflows: write`, so its push cannot change the workflows that test and publish the
release. Of the owner's `env` secrets, a release run is leased only `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY`,
the login of its runtime, and no `git` secret.

## Commands and routes

```sh
evo-agents hub release set --project evo-agents --repo evo-agents      # defaults for everything else
evo-agents hub release set --project evo-agents --at 06:30
evo-agents hub release status --project evo-agents --json
evo-agents hub release run --project evo-agents                         # check now
evo-agents hub release pause --project evo-agents
evo-agents hub release resume --project evo-agents
```

`release set` keeps what the schedule holds for each option left out, or its default the first time; `--path`
repeats and replaces the packaging paths whole. `set`, `pause`, `resume` and `run` need the admin role; `status`
needs a grant on the project. `--json` prints the hub's answer with the keys the CLI contract declares
(`tests/hub/golden/cli-contract.json`).

| Route | Role | What it does |
| --- | --- | --- |
| `GET /v1/projects/{p}/releases` | reader | the schedule, whether it is paused, its next check, the latest checks and the cycle open or stopped, with its runs, tags, CI and cost |
| `GET /v1/projects/{p}/releases/cycles` | reader | the cycles that ended, newest first (`limit` 1 to 100, `offset`) |
| `PUT /v1/projects/{p}/releases/schedule` | admin | sets the schedule; the caller becomes its owner; audited as `release.set` |
| `POST /v1/projects/{p}/releases/pause` | admin | pauses it: queued release runs cancelled, an open cycle with no run under way closed; audited as `release.pause` |
| `POST /v1/projects/{p}/releases/resume` | admin | lets it check again; audited as `release.resume` |
| `POST /v1/projects/{p}/releases/run` | admin | a check now, answered; audited as `release.run` |
| `POST /v1/worker/runs/{id}/release/branch` | the worker that holds the run | the branch `release/<version>` the run pushed |

Pausing cancels only the runs still queued: a run already under way is not stopped. A worker token gets 403 on the
project routes. Every run the hub queues is audited as `release.dispatch`, with the owner as actor.

The web's Releases page (`/p/{project}/releases`) shows the schedule, the next check and the last one, the cycle open
now with its tags, their CI and workflow runs, its runs and their cost, and the cycles before it. An
admin pauses, resumes and runs a check there. It shows a status only when something needs attention: the schedule
paused, the cycle stopped, or the App lacking Actions (`web/DESIGN.md`).

## Notices

| Notice or decision | When | Links to |
| --- | --- | --- |
| notice `release`, `details.event` `published` | the cycle's tag passed its CI, its job `publish` included | the Releases page; the version, the workflow run and the version's PyPI page in its body |
| notice `release`, `details.event` `actions_permission` | the hub cannot read the CI because the installation grants no Actions | the Releases page |
| decision of category `release` | the cycle stopped | the Inbox (`/inbox?decision=ID`), and the cycle's thread in the triage chat |

`docs/notifications.md` describes channels and deliveries. A failed release run opens no generic triage thread and sends
no `run_failed`: its cycle's stop does the asking.
