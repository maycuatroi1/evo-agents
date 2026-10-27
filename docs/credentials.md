# Credentials of a run

A worker that runs a plan step needs credentials: to fetch and push the run's repos, for its runtime to reach a
model, and for its agent to read and write the hub's memory. Up to 0.4 the daemon used whatever the machine had: the
owner's git credential helper and SSH keys, the runtime's own login, and `~/.evo/hub/token`, a machine token with
every grant of its owner. That is fine on a laptop only its owner uses. It is not on a machine others also
administer, where anyone with root reads those credentials and keeps them long after the run.

From 0.5.0 the hub keeps the credentials, sealed, and gives each run a **lease** of just what the run needs, for as
long as it runs. The daemon keeps leases in memory, hands them to git and the agent, and gives them back when the run
ends; the hub revokes what it can. A machine set up this way keeps only its worker token, which works on
`/v1/worker/*` alone.

`evo_agents/hub/credentials.py` holds the model both ends share; `evo_agents/hub/server/sealing.py` seals values,
`evo_agents/hub/server/secrets.py` holds the owner's routes, `evo_agents/hub/server/github_app.py` the GitHub App
client, `evo_agents/hub/server/credentials.py` the worker's lease route, `evo_agents/worker/credentials.py` the
daemon's side, and `evo_agents/worker/credential_commands.py` the commands a run calls on the daemon's socket (git's
helper, `evo-agents worker env`).

## Threat model

Two attackers shape this design.

- **Another administrator of the worker machine.** A shared Mac mini, a lab desktop, a server a team runs: root reads
  every file of the worker's user, its keychain once unlocked, and the memory of its processes. Credentials stored on
  the machine are theirs too, for as long as the credentials live.
- **Prompt injection into the agent.** The agent runs with `bypassPermissions` (or its runtime's equivalent) and
  reads untrusted text: issues, web pages, files of the repos. Whatever the agent's environment and home hold, an
  injected instruction can send somewhere.

Neither can be stopped from reading what a run is using while it runs: root reads the daemon's memory, and the agent
must have its credentials to work. What the design limits is **how much** they get and **for how long**:

- a run gets only credentials for its own project, its own repos and its own worker (its agent can still reach what
  the other runs of the same worker hold, as "What is still a risk" says);
- the GitHub token covers only the run's repos that its owner may push to on GitHub, with `contents: write` and
  `metadata: read`, and lives an hour;
- nothing is written to disk, so nothing outlives the run on the machine;
- the hub revokes the GitHub token when the run ends, and the owner can cut every credential at once by revoking the
  worker or deleting the secret.

## Where a lease comes from

| Provider | Kind | What the run gets | Lives |
| --- | --- | --- | --- |
| `secret` | `env` | an environment variable of the agent, such as `CLAUDE_CODE_OAUTH_TOKEN` | until the owner deletes or replaces the secret, or its `expires_at` |
| `secret` | `git` | what git's credential helper answers for origins under `url_prefix`: `username` (default `oauth2`) and the value as password, e.g. a GitLab project access token | as above |
| `github-app` | `git` | an installation token of the hub's GitHub App for the run's repos on github.com | one hour, revoked when the run ends |

A **secret** belongs to the member who wrote it. Its value goes into the hub once (`PUT /v1/secrets/{name}`, the web
page Secrets, or `evo-agents hub secret set` reading stdin) and never comes back out through the owner's routes:
listing shows names, kinds, targets, bindings and dates, never the value. A secret is bound to one or more projects
the owner holds writer on, and optionally to some of the owner's workers or worker pools. A hub admin sees only their own secrets.

The **GitHub App** is the hub's own (`EVO_HUB_GITHUB_APP_ID`, `EVO_HUB_GITHUB_APP_PRIVATE_KEY`). For each owner of a
run's github.com repos the hub finds the installation (`GET /repos/{owner}/{repo}/installation`) and asks for a token
for those repos only (`POST /app/installations/{id}/access_tokens`). A run over repos of two GitHub owners gets two
tokens. A repo the App is not installed on gets none, and the run is told why: "the GitHub App is not installed on
owner/repo".

The App is installed on GitHub accounts, not given to members, and a project's admin may register any origin for a
repo of the project. So before a token goes out the hub asks GitHub whether the run's owner may push to each of its
repos: `GET /repos/{owner}/{repo}/collaborators/{login}/permission`, with the installation token just made, which
opens the repo, where `login` is the owner's login on the hub, their GitHub login. Only `write`, `maintain` or `admin`,
for the GitHub account that signed in to the hub (its id is compared, so a login renamed and taken by someone else
since does not pass), keeps the repo in the token. A repo the owner may not push to gets no token, and the run is told
why: "LOGIN cannot push to owner/repo on GitHub: their role there is read, and pushing needs write". The token that
asked opened it too, so it is revoked and a token is made again for the other repos of the installation. An answer
that allows is kept 5 minutes per login and repo (`PERMISSION_CACHE_SECONDS`); a refusal is not kept, so push access
granted on GitHub counts at the run's next ask.

The hub signs each of these calls with a JWT of the App (RS256, issued 60 seconds back, ending 9 minutes
ahead, inside the 10 minutes GitHub allows), keeps an installation it found for 10 minutes per repo, and looks again
every time for a repo the App is not installed on, so installing the App counts at the run's next ask. A token is
revoked with itself (`DELETE /installation/token`).

Values are sealed with AES-256-GCM under `EVO_HUB_SECRETS_KEY` (32 bytes, base64url). Each sealed value records the
`key_id` of the key that sealed it (the first 8 hex digits of its SHA-256) and is bound by its associated data to
where it belongs (`secret:{owner_id}:{name}:{kind}`, `lease:{id}`), so a sealed value copied to another row does not
open. Without the key the hub refuses to write secrets (503) and leases nothing. The key is not in the database and
not in its backups.

## The owner's routes

`evo_agents/hub/server/secrets.py`, with a machine token or a web session; a write made with the session cookie needs
`X-Evo-CSRF`, as every write does. A worker token gets 403 here.

| Route | What it does |
| --- | --- |
| `PUT /v1/secrets/{name}` | creates the caller's secret `name`, or replaces it whole, value included; `created` says which |
| `GET /v1/secrets` | the caller's own secrets that are not deleted, without their values |
| `DELETE /v1/secrets/{name}` | deletes it softly: the row stays, its sealed value and bindings go, its live leases are revoked |

The body of a PUT:

- `kind`: `env` with `env_var`, an upper-case variable that is none of `DENIED_ENV` and starts with none of its
  prefixes (`EVO_`, `GIT_`, `LD_`, `DYLD_`, `PYTHON`), or `git` with `url_prefix`, an https URL without a user,
  password, query or fragment, kept in the form of `normalize_origin`, and `username` (`oauth2` when left out), or
  `forge` with `url_prefix` alone (see [Forge secrets](#forge-secrets-the-hubs-own)). A field of another kind is
  refused.
- `projects`: one or more projects the caller holds writer on; a project the caller holds less on is 403, one they
  cannot see 404.
- `workers`: optional names of the caller's own workers that are not revoked; any other name, another member's worker
  included, is 403, also for a hub admin. Left out, the secret goes to any worker of its owner. The bindings are each
  project on each named worker.
- `pools`: optional names of the caller's own worker pools (`docs/workers.md`, Pools), in any case; another member's
  pool, or a name the caller has no pool of, is 403. The bindings are each project on each pool
  (`secret_bindings.pool_id`, schema 0150), and a pool covers each of its nodes, those that join after the binding
  too. A body naming both `workers` and `pools` is 422: a pool covers its nodes already. A `forge` secret takes
  neither.
- `expires_at`: optional, with a time zone, in the future.
- `value`: 1 to `MAX_SECRET_BYTES` (16384) bytes of UTF-8, without NUL; one line for kind `git`.

A name is 1 to 64 characters of `a-z`, `0-9`, `.`, `_` and `-`, starting with a letter or digit, unique among the
owner's secrets that are not deleted; a deleted secret's name is free again. A member keeps at most
`MAX_SECRETS_PER_OWNER` (200) secrets (409 past that). A hub admin's routes are the same: they list, change and delete
only their own secrets, and `/v1/admin/stats` counts the rows of `secrets` without showing one. Each write adds an
audit row `secret.put` or `secret.delete` whose target is the secret's id, `secret:<id>`, never its name: hub admins
read the audit (`/v1/admin/audit`), and a secret's name is its owner's alone, as their list and a run's credentials
are. No audit row, log line or error carries a value, and a 422 never repeats the input.

On the web, the Secrets page (`/secrets`) lists the visitor's secrets and adds, replaces and deletes them. Its value
field is a password field the page never fills: a replace asks for the value again, and the field is emptied as soon
as the form is sent, whatever the hub answers.

From the command line (`evo_agents/hub/cli_secrets.py`):

```sh
pbpaste | evo-agents hub secret set claude-oauth --kind env --env-var CLAUDE_CODE_OAUTH_TOKEN --project demo
evo-agents hub secret set gitlab-docs --kind git --url-prefix https://gitlab.example.org/group \
  --project demo --worker mac-mini --expires 2027-09-15
evo-agents hub secret list --json
evo-agents hub secret delete gitlab-docs
```

`secret set NAME (--kind env --env-var VAR [--environment test] | --kind git --url-prefix URL [--username U] | --kind
forge --url-prefix URL) --project P [--project P ...] [--worker NAME ... | --pool NAME ...] [--expires YYYY-MM-DD]`
has no flag for the value. `--pool` binds it to the nodes of your pools instead of to named workers, never both. `--environment test` labels an env secret for tests: a run whose principal's K7 is `auto:test_secrets` is leased
those alone, besides its runtime's sign-in (`docs/autonomy.md`). It reads the value from stdin when stdin is not
a terminal, dropping one final line break, and otherwise asks for it with `getpass`, which does not echo it. The
arguments are checked first, then that the machine is signed in, and only then is the value read, so a mistake never
costs a paste. The value is checked for what the hub would refuse (empty, over `MAX_SECRET_BYTES`, a NUL, more than
one line for kind `git`) without being shown, and no output or error of the command carries it. `--expires` ends the
secret at 00:00 UTC of that day, as GitLab ends an access token on its expiry date, so the token's own date fits.

## Forge secrets: the hub's own

A secret of kind `forge` is a GitLab project or group access token (scope `api`, role Maintainer) with which the hub
itself drives the merge requests of a project's repos under its `url_prefix`: it reads a merge request, its diff and
the pipelines of a commit, rebases it, merges it with the sha it checked, plays a manual job, and opens a revert
(`evo_agents/hub/server/gitlab.py`). No run ever gets it. `docs/merge-queue.md` says what the hub does with it.

- Writing one needs the admin role on each of its projects, where `env` and `git` need writer. It needs `expires_at`,
  takes no `username` (GitLab reads the token from the `PRIVATE-TOKEN` header) and no `workers`, holds one line, and
  its `url_prefix` is not on github.com, where the hub merges by the Curator's tiers. Migration 0080 widens the checks
  of `secrets` for it; the table has no new column.
- It is never leased: the lease query reads kinds `env` and `git` alone (`credentials.LEASED_KINDS`). No route of the
  worker protocol, `/mcp` or the API reads its value; `GET /v1/secrets` shows its owner its name and target, as for
  every secret, and a run's leases never name it.
- Only the hub's job worker opens it, right before it calls GitLab (`evo_agents/hub/server/forge.py`,
  `forge_token`): of the forge secrets bound to the project, not deleted, not past their end, whose owner still holds
  the admin role on the project, the one whose `url_prefix` covers the repo's origin the longest, then the one written
  last. The value stays in the memory of those calls and travels in the `PRIVATE-TOKEN` header alone; no log line,
  error, event or audit row holds it.
- A project with no forge secret for a repo's origin gets no token: its merge requests on GitLab stay open for a
  person, as before.
- The web's Secrets page writes one too: the kind Forge asks for the URL prefix (not on github.com), the projects the
  visitor administers and an end date, offers no worker, and takes one line.

```sh
pbpaste | evo-agents hub secret set gitlab-merge --kind forge --url-prefix https://gitlab.example.org/group \
  --project demo --expires 2027-09-15
```

## Which leases a run gets

K7 of the run's principal narrows what follows (`docs/autonomy.md`, `server/lease_autonomy.py`): under
`auto:run_branch`, `auto:test_secrets` or deny a run gets a GitHub token that writes only for a repo whose ruleset keeps
the App off its default branch, a token that reads for that repo alone elsewhere, a git secret only for an origin whose
default branch on GitLab no one may push to, and no env secret but its runtime's (and those labelled test under
`auto:test_secrets`). A run without an autonomy snapshot, or at auto or notify, gets what follows unchanged. A lease
that held anything back writes the audit row `autonomy.refused` with the class, the mode and the source; every K7
decision at a lease also writes `autonomy.decided` (the floor S1). An env secret carries an environment label
(`secrets.environment`, schema 0114): none, or `test` with `hub secret set --environment test`; `hub secret list`
shows it.

`evo_agents/hub/server/credentials.py`. `POST /v1/worker/runs/{id}/credentials`, with the worker's token and the
protocol header, for a run the worker holds: one of the held states, `waiting` of a plan run among them. Any other run,
another worker's or one that ended, is 404, as for a state report; a machine token or a web session gets 403, as on
every worker route. The run's owner, the member who dispatched it, must still hold writer on the run's project: once
an admin deletes that grant or lowers it to reader, the ask is 403 ("LOGIN, who dispatched run N, no longer holds the
writer role on project P: the run gets no credentials"), even for a run the worker still holds. The hub takes the
run's repos (the run's repo for a step run, the repos of the plan run, those of the plan's steps not done when it was
dispatched, the repos a chat run works in) and their origins from `project_repos`, compared in the form of
`normalize_origin` (`git@host:path` and `ssh://git@host[:port]/path` included). It then leases what the run's owner
bound to the run's project, bound to no worker, to this one or to this worker's pool, and not past its
`expires_at`:

- one `env` secret per variable, the run's account for it (section "Accounts" below): with one secret per variable
  and no policy, that secret, as in 0.13;
- each `git` secret whose `url_prefix` covers one of those origins, the longest prefix for each origin;
- a GitHub App token for the origins on github.com that no `git` secret covers and that the run's owner may push to
  on GitHub, one per installation: a lease of kind `git` named `github-app:<owner>` for `https://github.com/<owner>`,
  with the username `x-access-token`. Its permissions are `GITHUB_PERMISSIONS`, plus `workflows: write`
  (`GITHUB_WORKFLOW_PERMISSIONS`) when the installation of the workers' App holds it: GitHub refuses a push that
  creates or updates a file of `.github/workflows` without it. To let runs push such a change, give the App the
  Workflows permission (read and write) in its settings and accept it on the installation; a run asks its token at
  its start, so a run that began before keeps its token without it until that token is renewed. A run of the
  Curator never gets it.

A repo's origin is the one the run's project registers for it, else the one another project registers for a repo of
that name, among the projects the run's owner holds a grant on, when they agree on it (`repo_origins`).

The answer is `{leases, missing, repos}`. Each lease carries `id`, `kind`, `provider`, `name`, `env_var`, `url_prefix`,
`username`, `value` and `expires_at`, as `Lease.from_json` reads them. `repos` names each repo of the run with its
`origin`, `path` and the `project` that registered them, which a worker without a checkout of it clones from
([Repos a run clones](workers.md#repos-a-run-clones)). `missing` names each repo whose origin nothing
covers, with its origin (null when the project registered none) and the reason: no origin, no `git` secret covering
it, "the GitHub App is not installed on owner/repo", "LOGIN cannot push to owner/repo on GitHub: ...", no GitHub
App configured, GitHub failing. Without
`EVO_HUB_SECRETS_KEY` nothing is leased and every repo is missing with that reason.

A **run of the Curator** gets less, and from elsewhere (`changes.curator_policy`): a review run, a judge run, and any
run of a plan the Curator made (its Builder). Its GitHub token comes from the Curator's own App, evo-agents-curator
(`EVO_HUB_CURATOR_APP_ID`, `EVO_HUB_CURATOR_APP_PRIVATE_KEY`), never from the workers' App: the ruleset of each repo's
default branch blocks updates and lists the workers' App and the repo's admins as bypass actors, not the Curator's
App, so a token of the Curator's App cannot push to, or merge into, a default branch, while the member's own plan runs
keep the workers' App and their default branch. Without the Curator's App configured a run of the Curator gets no
GitHub token at all ("a run of the Curator gets its GitHub token from the Curator's App alone, and this hub has none:
... not set"). A review run's and a judge run's token reads only. No `git` secret answers for an origin on github.com
in a run of the Curator; for other origins (GitLab) it gets only the `git` secret the charter names (`git_secret`, the
owner's, which should hold the Developer role and so cannot push to a protected default branch either), and of the
`env` secrets only those the charter lists (`env_secrets`). The hub's own calls for the Curator's pull requests (open
one, read its files and CI, write the Judge's check run, merge) use tokens of the workers' App made for that one call,
with the permissions it needs (pull requests, contents read to open one, checks, commit statuses), and revoked after
it: the tokens a worker's run gets keep `GITHUB_PERMISSIONS`.

A **release run** ([releases.md](releases.md), "The run on the worker") is guarded as a run of the Curator is
(`RunCredentials.guarded`): its agent gets no credential helper and no lease that pushes, and only the daemon's own git
uses the token, with a ticket of one command, to push `release/<version>`. Its preflight checks that push against the
lease alone, never the machine's own credentials, and fails the run when no lease covers it. Of the owner's secrets it
is leased only the `env` secrets of its runtime's login, `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY`
(`RELEASE_ENV_VARS`), and no `git` secret: its repo must be on GitHub, where it gets a token of the workers' App with
`GITHUB_PERMISSIONS` (contents write, metadata read) and never `workflows: write` (`NO_WORKFLOW_KINDS`), so its push
cannot change the workflows that test and publish the release. The hub, not the run, merges and tags, with tokens
of the workers' App made for each use: contents and pull requests write for the landing, actions read to follow the
tag's CI, actions write to run its failed jobs again. The App needs the Actions permission (read and write) for those
two, accepted on the installation; until then the hub reports `actions_permission` ([releases.md](releases.md), "The
GitHub App").

A run's agent profile ([hub.md](hub.md), Agent profiles), at the version the run went under, narrows all of this:
of the secrets above, a run is leased only those whose names the profile's credential allowlist holds, and a token of
the hub's GitHub App only when it holds `github-app`; a repo on GitHub it would have covered is missing, with the
profile as the reason. The intersection is taken on every ask, so a secret the owner adds later is not leased to a
profile that does not name it, and an account picked by hand, or picked by the rules, is one the profile allows. A
profile with every permission, such as a project's default, changes nothing.

A **chat run** ([hub.md](hub.md), Group chats) is leased as any run: its repos are those of its project that its
worker has a checkout of, and its GitHub token has `GITHUB_PERMISSIONS`, as a step run's. The worker pushes only the
branch `chat/<run>`, never a default branch and never forced, and no run merges. Its owner is the person at the
origin of the chain: the person whose message in the chat mentioned the agent, and, for a run that another agent's
mention made (`evo-agents worker chat mention`, hop 2 or 3), the owner of the run that asked, carried down the chain.
So the run is leased that person's secrets, on that person's worker, narrowed by the profile of the agent it runs as;
never those of another member of the chat, or of whoever added the agent to it. That person must hold writer on the
profile's project when the hub makes the run and on every ask after, as for any run.

A **task run** ([plugins.md](plugins.md), Agent types) is leased as any run too: its repos are every repo of its
project when the plugin handed the task, each on the branch `task/<id>` of the run the dispatch made (a lost run's
next attempt keeps it), and its GitHub token has `GITHUB_PERMISSIONS`, as a step run's. Its owner is the agent type's
`run_as`, the member the project's admin named when enabling the type, never the plugin: the plugin's token opens the
plugin API alone and leases nothing. So the run is leased `run_as`'s secrets, on a worker of `run_as`, narrowed by the
type's profile, and `run_as` must hold writer on the project at the dispatch and on every ask after. The worker
pushes only the run's branch, never a default branch and never forced, and no run merges; the hub opens the draft
pull request with the workers' App. The variables an ACP runtime of a plugin names (`secrets`, [plugins.md](plugins.md),
Runtimes) come from these `env` leases, or the worker's own environment, in a task run as in any other.

Each call records its leases in `credential_leases` and adds one audit row `credential.lease` naming the run, the
secrets by id (`secrets=12,15`), the App's accounts, the repos and, unless it is the project's default, the run's
profile (`profile=@<handle>:v<version>`), never a value; each lease row records the profile and version too.
`credential.revoke` names the secrets by id too. A secret is leased once per run and worker: asked again,
the same lease comes back with the secret's value as it is now. A GitHub token stays sealed in its lease, bound to the
lease's id, until it expires, so the hub can revoke it. Asked again, a run gets the same tokens while each has
`GITHUB_TOKEN_REFRESH_SECONDS` or more left, and new ones in new leases after that; the tokens they replace go on
working until they expire, so a push that took one is not cut off. GitHub is asked between two transactions, never
while the run's row is locked; when the run ended in between, the tokens just made are revoked and the answer is 404.

## Accounts

A member may keep several `env` secrets for the same variable bound to the same project, such as two subscriptions of
the agent's runtime (`claude-a`, `claude-b`); each is an **account**, and a run gets one of them per variable
(`evo_agents/hub/server/account_choice.py`, migration 0027). The hub picks it in the transaction that leases it, with
the run's row locked and the accounts it weighs locked in the order of their ids, so two runs asking at once cannot
take the same turn:

1. the account the run took already: a run keeps its account on every ask for its leases and in every step, and a run
   that resumes another takes the one that run took;
2. else the accounts bound to this worker or to its pool (for a run of the Curator: only those its charter lists in `env_secrets`),
   then the others bound to the project, by the strategy of the owner's policy for the project:
   - `round-robin` (the default): each run takes the account whose turn it is;
   - `weighted-round-robin`: each account takes runs in proportion to its `weight`; weight 0 leaves the pool;
   - `fill-first`: the lowest `priority` (then the name) while it is usable;
   an account its owner paused, or one cooling down until `cooldown_until`, is skipped;
3. when every account left cools down, the one whose cooldown ends first, and a `system` event of the run says so;
   when every one is paused, none, and an event says that. An event names the account by its secret's id, never by
   name or value.

A member may also pick the account by hand when dispatching: `account` on the body of `POST .../runs`,
`.../plan-runs` and `.../author-runs` (`--account NAME` on `evo-agents hub run dispatch|plan|author`, and the Account
select of every dispatch form on the web) names one of their env secrets bound to the project. The hub checks it in the
transaction that queues the run and records it in `run_accounts` with how `manual`, so step 1 above keeps it on every
lease; an account picked by hand is never failed over to another one, and its rotation does not move. A name that is
not an env secret of the dispatcher's is 404; one past its end, paused, not bound to the project, bound only to other
workers, or bound to some workers while the run is not pinned to one of them, is 422 saying why, and nothing is queued.
`POST .../runs/{id}/rerun` keeps the account picked by hand for the run it reruns, checked again the same way; its body
(`--account NAME` or `--account-by-rule` on `hub run rerun`, the Rerun dialog on the web) names another one or asks the
rules (`account_by_rule`). A body without `account` dispatches as before. Audit rows of the dispatch end with
`account=secret:<id>`.

When the agent of a headless run (a step run, a plan run or an author run of Claude Code) hits its account's usage
limit, the worker says so: the CLI's `rate_limit_event` with status `rejected`, or a result whose error reads as a
usage limit. It calls `POST /v1/worker/runs/{id}/rate-limit` with the lease of the account (`lease_id`), the
`status`, the limit's `rate_limit_type`, the reset time (`resets_at`) when the CLI gave one, and the usage
(`utilization`, `windows`); an event with status `allowed` or `allowed_warning` is reported the same way and only
keeps the usage on the account (`accounts.last_usage`). The hub keeps it window by window: the report's `windows`
replace those of the same name, a window it does not list keeps what an earlier report said of it, and `status`,
`rate_limit_type`, `resets_at` and `utilization` are the newest report's. `last_usage_at` is the time of the newest
report, so a window kept from an earlier one may be older than that. A window whose reset has passed stays until a
report lists it again, and the web reads it as reset, at 0%. A report that is not `rejected` and comes while the
account cools down from a `rejected` one changes nothing, no window either: it is older than the limit. A share
(`utilization`) is a fraction of the limit, 0.35 for 35%, as the CLI gives it; the route takes 0 to 100 and keeps the
value as sent, and the web and `hub account list|show` read a value above 1 as the limit reached (100%), never as a
percent. On `rejected` the hub cools the account down until the reset,
else for the policy's `cooldown_seconds`. When the run may fail over (failover on, its account not picked by hand,
fewer than `max_failovers` moves, another account ready by the rules above), the hub moves its `run_accounts` row to
the next account, counts one more failover, revokes the old lease and answers `failover`; the worker asks for the
run's leases again, swaps the value in the agent's environment and in the run's socket, stops the agent and goes on in
the same session with the new account. Else it answers `stop` with the reason and the earliest time an account is
ready again, and the worker ends the run `failed` with cause `rate_limit` and that time in its error. A `system`
event of the run records each switch or stop, and the audit row `account.failover` each switch
(`... run:<id> env=<variable> from=secret:<id> to=secret:<id>`): both name the accounts by secret id, never by name
or value. A worker of an older release never calls the route, and its runs fail as before. An interactive pane only
shows the event: the person at it switches by hand.

An account whose token the API refused (the agent's turn ended with "Failed to authenticate. API Error: 401 OAuth
access token is invalid." or the like) is reported with status `unauthorized`: the hub keeps no usage, cools the
account down for the policy's `cooldown_seconds` with the reason "authentication failed, reported by run N: log in again
with evo-agents hub account login", and moves the run or stops it by the same rules; a run it stops fails with cause
`account_auth`. For a setup-token account there is no state for a token that needs a new login: the account shows as
cooling, with that reason, and a token renewed with `evo-agents hub account login` is taken once the cooldown ends.
An OAuth account whose refresh token the endpoint refuses has the state `needs-relogin` (see "Adding and renewing an
OAuth account").

`run_accounts` records which account each run took for each variable and how (`manual`, `worker`, `charter`, `pool` or
`cooling`); `accounts` holds each account's weight (0 to 1000, default 1), priority (0 to 1000, default 0), pause,
cooldown, rotation and the last usage a run reported (`last_usage`, `last_usage_at`); `account_policies` holds a
member's policy for a project: `strategy`, `cooldown_seconds` (60 to 604800, default 3600), `failover` (default on) and
`max_failovers` (0 to 20, default 3).

The routes are the member's own, a hub admin included, and never carry a value:

- `GET /v1/accounts`: the caller's env secrets as accounts, with `name`, `env_var`, `projects`, `workers`, `weight`,
  `priority`, `paused_at`, `cooldown_until`, `cooldown_reason`, `last_leased_at`, `expires_at` and `state` (`ready`,
  `cooling`, `paused` or `expired`), and its figures (`evo_agents/hub/server/account_views.py`): the last usage a run's
  CLI reported (`last_usage`, `last_usage_at`), the runs that took it in the last 24 hours and 7 days (`runs_24h`,
  `runs_7d`: a run took it at its first lease of it) and their estimated cost in USD from their usage (`cost_24h_usd`,
  `cost_7d_usd`: a run that failed over counts with each account it took), and the runs a worker holds with it now
  (`held_by`); `GET /v1/accounts/{name}` gives one of them, 404 for a name that is not the caller's;
- `PATCH /v1/accounts/{name}` with `weight`, `priority` or `paused`: 404 for a name that is not the caller's; audit
  `account.set` names the secret by id;
- `GET /v1/account-policies`, `GET|PUT|DELETE /v1/account-policies/{project}`: the caller's policy for a project they
  may read (the defaults, `updated_at` null, when they set none); `PUT` needs the writer role; audits
  `account.policy-put` and `account.policy-delete`;
- `GET /v1/projects/{project}/runs/{id}/accounts`, for the member who dispatched the run alone (403 for anyone else,
  as for its credentials): per variable, the account the run has now by name, how it came by it, its failovers, and
  each usage limit it hit (`events`: `failover` with the account it moved `to`, or `stop`), read from the run's
  `system` events, which carry `account_event`, `to_secret_id` and `cooldown_until` besides the secret ids.

Each account has a **provider** and a **category**, read from its variable by a table in
`evo_agents/hub/account_providers.py` (no column, no migration): `CLAUDE_CODE_OAUTH_TOKEN` is `claude`,
`ANTHROPIC_API_KEY` is `anthropic` and `OPENAI_API_KEY` is `openai`, the three of category `model`; `GITLAB_*` is
`gitlab`, `GITHUB_*` and `GH_TOKEN` are `github`, `NOTION_*` is `notion`, `LARK_*` and `FEISHU_*` are `lark`, a variable
ending in `_SSH_KEY` or starting with `SSH_` is `ssh`, and any other variable is `other`; all of these are category
`service`. `GET /v1/accounts` and `GET /v1/accounts/{name}` carry both, and so do `account list --json` and
`account show --json`; `account list` prints the provider in its own column. Only a `model` account has a cost: a run's
cost (its usage, as above) is counted on the model accounts the run took, and `cost_24h_usd` and `cost_7d_usd` are null
for a `service` account, which `account list` shows as `-` and `account show` leaves out; a run that took a Claude
account and a GitLab token costs the Claude account alone. `runs_24h` and `runs_7d` count for every account.

The web's Accounts page (next to Secrets in the sidebar) shows the same and edits weight, priority, pause and each
project's policy; the run page shows the run's accounts and their failovers. Under the row of each Claude account, and
of any account a run reported usage for, the page draws a bar per window the hub kept, as claude.ai's Usage page does:
"Session (5 hours)" for `five_hour`, "Weekly, all models" for `seven_day`, "Weekly, Opus" for `seven_day_opus` (any
other name in words), each with its share used and when it resets ("Resets in 2 hr 51 min" within a day, "Resets Tue
7:00 AM" past that), and above them how long ago a run reported it: the bars change only when a run uses the account.
A bar turns amber from 80% and red at 100% or at the limit the CLI rejected. A Claude account no run reported for says
"No usage reported yet". `evo-agents hub
account list|show|set|pause|resume|policy` call them:

```sh
evo-agents hub account list                                   # state, weight, priority, usage, runs, cost, holders
evo-agents hub account show claude-a                          # one account and its figures, never the value
evo-agents hub account set claude-b --weight 3 --priority 1
evo-agents hub account pause claude-a                         # a run that holds it keeps it
evo-agents hub account resume claude-a
evo-agents hub account policy demo --strategy weighted-round-robin --cooldown 1800 --no-failover
evo-agents hub account policy demo --reset                    # the defaults again
```

### Adding and renewing a Claude account

A Claude account is an `env` secret for `CLAUDE_CODE_OAUTH_TOKEN` whose value is a token of `claude setup-token`.
Anthropic's [Authentication](https://code.claude.com/docs/en/authentication) page, section "Generate a long-lived
token", says that command opens the browser sign-in of `/login`, prints a token that lasts a year, uses the Claude
subscription (Pro, Max, Team or Enterprise) and may only call the model. There are three ways to put one on the hub,
and each ends in `PUT /v1/secrets/{name}` with kind `env` and that variable:

```sh
evo-agents hub account login claude-b --project demo                  # runs claude setup-token, saves what it prints
evo-agents hub account login claude-b --project demo --worker mac-mini --expires 2027-09-15
evo-agents hub account login claude-b                                 # renew: a new token, same projects and workers
pbpaste | evo-agents hub account login claude-b --project demo        # a token made elsewhere, read on stdin
evo-agents hub account login claude-b --paste                         # asks for the token without echo
```

`account login NAME [--project P ...] [--worker W ... | --pool NAME ...] [--expires YYYY-MM-DD] [--paste] [--json]`
(`evo_agents/hub/cli_secrets.py`) first does every check that needs no token: the name, the day, that the machine is
signed in, the writer role on each project (`GET /v1/auth/whoami`) and that each worker is one of the caller's
(`GET /v1/workers`), or each pool (`GET /v1/worker-pools`). So a refusal never throws away a token just made. Only then does it run `claude setup-token`
unchanged, on a pseudo-terminal of its own (`evo_agents/hub/setup_token.py`). The member signs in to Claude and
approves in the browser it opens, and the command reads the token off the screen, joining it again where the
terminal's width wrapped it. Claude Code 2.1.296 prints the token from column 1 up to the right margin and goes on at
column 1 on the next row, so in a terminal narrower than 110 columns the token takes two rows or more. When the
command cannot tell how the rows join, it asks for the token rather than keep the first row alone, since a token cut
short fails every run with a 401. The token never reaches the member's screen: from its first bytes on, the relay
passes on only the escape sequences that set or reset the terminal's modes, and `claude` is stopped if it still runs 5
seconds later. The command writes the token, prints the account as `account show` does (on stdout; the progress lines
go to stderr), and with `--json` prints the `Account` schema instead.

When the screen held no token (a sign-in cancelled, a release of Claude Code that prints it another way), the command
says so and asks for it at a prompt that does not echo, where Enter alone stops. With `--paste`, or when stdin is not a
terminal, it does not run `claude` and reads the token as `secret set` reads a value: from stdin, one final line break
dropped, else with `getpass`. On a system without `pty` (Windows), `claude setup-token` uses the terminal itself, so the
token shows there, and the member pastes it. A machine without `claude` on PATH gets an error that says how to do it by
hand: install Claude Code, or run `claude setup-token` where it is installed and give the token to `--paste` or on
stdin. The command refuses an API key of the Anthropic Console (`sk-ant-api...`) and points to `ANTHROPIC_API_KEY` with
`hub secret set`. It also refuses a token with a space or line break inside, a new account without `--project`, and a
name that is already a secret of another kind or another variable. No output, log line, argument or error carries the
token.

The account ends at 00:00 UTC of `--expires`, or by default of today (UTC) plus a year less a day, so the hub takes it
out of use at least a day before the token itself stops working. A day already past is refused before `claude` runs.
From `expires_at` on, the account's state is `expired`, and no run takes it.

Renewing is the same command with the name of an account the caller has. The command says first that it replaces that
account's token. Without flags it sends the account's projects and workers again; `--project` and `--worker` each
replace only their own binding. The `PUT` keeps the secret's id, so the account's weight, priority, pause, cooldown and
runs stay, and only the token and `expires_at` change.

`account show` has an `Expires:` line: `never`, the time in UTC, or for an account that ends within
`RENEW_WITHIN_DAYS` (30) days (`evo_agents/hub/accounts.py`) how many days are left and the command that renews it;
for one already past, that no run takes it. `account list` adds such a line under its table for each account that
ends within 30 days or has ended. For a Claude account the line names `evo-agents hub account login NAME`, and for an
account on another variable `hub secret set`.

On the web, the Accounts page has **Add Claude account** in its header and in its empty state. The dialog asks for the
name, the projects (at least one, among those the visitor holds writer on), the workers (optional; none means any of
theirs), the token in a masked field, and the day it ends, a year from today less a day by default. It shows the steps
of `claude setup-token` and the matching `evo-agents hub account login` command to copy, for a member who would rather
have the CLI read the token. A name that is a Claude account already shows a note that saving replaces its token, with
a button to renew it instead; a name that is another kind of secret is refused, since the `PUT` would replace it whole.
The dialog refuses an API key, as the CLI does, and forgets the token as soon as it is sent. Each row for
`CLAUDE_CODE_OAUTH_TOKEN` has **Renew token**, which opens the same dialog with the name, projects and workers locked,
so only the token and the end change; to move an account, replace it on the Secrets page or pass `--project` or
`--worker` to `account login`. A token that ends within 30 days shows "Renew before" its day in the State cell, and a
notice above the table lists each one with a Renew token link; an account past its end shows "Ended" and its day.

`hub secret set` still writes an account, with the value on stdin or at a prompt. It sets no `expires_at` unless given
`--expires`, so give it the day the token ends, or the hub keeps leasing the token after it has stopped working:

```sh
pbpaste | evo-agents hub secret set claude-b --kind env --env-var CLAUDE_CODE_OAUTH_TOKEN --project demo \
  --expires 2027-10-13
```

### Adding and renewing an OAuth account

`evo-agents hub account oauth-login NAME [--project P ...] [--worker W ... | --pool NAME ...] [--no-browser] [--paste] [--json]` signs a
Claude account in with Claude's own OAuth flow (authorization code with PKCE, `evo_agents/hub/claude_oauth.py`), as
Claude Code and CLIProxyAPIPlus's `--claude-login` do, on the member's machine. The command prints the address on
claude.ai and opens it; once the member approves, the browser goes back to `http://localhost:54545/callback`, which the
command answers. Without a terminal, with `--paste`, or when the port is taken, the member gives the code claude.ai
shows (a bare code, `code#state`, or the address the browser went to) on stdin or at a prompt without echo. The command
trades the code at the token endpoint for an access token, a refresh token and the access token's lifetime, in memory
alone, and writes them with `PUT /v1/secrets/NAME`: the access token as the value, the rest as the field `oauth`
(`refresh_token`, `access_expires_at`, and the account's and organization's ids and address when the endpoint gave
them), which only an env secret for `CLAUDE_CODE_OAUTH_TOKEN` may carry. `EVO_CLAUDE_OAUTH_TOKEN_URL` and
`EVO_CLAUDE_OAUTH_AUTHORIZE_URL` name other endpoints, for a test.

The hub seals the whole set (access token, refresh token, end of the access token, identity) as one JSON object under
the associated data `account-oauth:{secret id}` and keeps it in the `oauth_sealed`, `oauth_nonce` and `oauth_key_id`
columns of the account's `accounts` row (schema 0066); no column holds a token in the clear, and no response shows one.
`expires_at` of the secret is the end of the access token. An account shows `auth: oauth` in `account list|show`
(`token` for any other); a write of the secret without `oauth`, or its deletion, drops the sealed set. Running the
command again for an existing NAME replaces the tokens alone, as `account login` does: same secret id, so weight,
priority, pause, cooldown and runs stay.

#### Refresh at lease time

An access token lives about eight hours. When a run asks for its credentials (`POST /v1/worker/runs/{id}/credentials`),
the hub first looks at each OAuth account the run may be leased (`evo_agents/hub/server/account_refresh.py`). One whose
access token ends in under five minutes, or has ended, is traded at the token endpoint for a new one with its refresh
token. Anthropic rotates the refresh token at every use, so the new access token, the new refresh token and the new end
are stored together in one transaction: the sealed set and the secret's value (the lease hands out the value as
`CLAUDE_CODE_OAUTH_TOKEN`, as for any account, and the worker's path is unchanged: an environment variable, no
credential file, the daemon's `ANTHROPIC_API_KEY` dropped). A token with more than five minutes left is leased as it is,
with no call out. The secret's row is locked during a refresh, so runs of one account that lease together refresh once:
the others wait, read the set the first stored and find it good. A refresh that fails (the refresh token revoked, the
endpoint down) changes nothing and logs the reason without a token; a token that has not ended is still leased, one that
has ended is not. The endpoint and client id are in `evo_agents/hub/claude_oauth.py` (`EVO_CLAUDE_OAUTH_TOKEN_URL`
names another endpoint, for a test).

#### needs-relogin

A refresh the endpoint answers by refusing the refresh token (revoked, ended) marks the account `needs-relogin`: a
cooldown that only a new sign-in ends, with the reason "needs-relogin: the OAuth refresh token was refused; run
evo-agents hub account oauth-login again". `account list|show` and the Accounts page show the state, the account is
left out of the choice for dispatch, candidates and triage, and the runs that held it fail over to another account as
for a spent limit (a run with none left fails with cause `account_auth`). An account whose access token has lapsed but
whose refresh token is still good is not in this state: it is usable, and the next lease refreshes it. Running
`oauth-login NAME` again stores the new tokens and ends the cooldown; a cooldown for a usage limit stays. A transient
failure of the endpoint (down, 5xx) does not mark the account.

### Accounts of an agent

An **agent account** is an `env` secret that an agent profile owns (`secrets.profile_id`, no member owner, migration
0104), for runs under that agent to use whoever dispatches them: an admin of the profile's project adds it from the
agent's page or with `evo-agents hub agent account` (`evo_agents/hub/server/agent_accounts.py`), names the workers it
is bound to (the admin's own that are not revoked, or shared workers serving the project), and gives it a weight, a
priority and an end like any account. The hub keeps the value sealed under its own associated data
(`agent-secret:<profile id>:<name>`) and never hands it back by any route. Only `env` accounts exist for an agent: git
secrets and the GitHub App token of a push stay the dispatcher's.

The rule, per variable, for a run under a named agent P (not the project's default profile, not a run of the Curator)
on worker W, for every kind of run (step, plan, author, chat at any hop, 1:1 chat, agent type `run_as`) and every
project the agent's project axis lets it run in:

- **P has a live account for the variable bound to W**: the run is leased one of those, chosen by weight, priority,
  pause, cooldown and `DEFAULT_POLICY` (round-robin, a cooldown of an hour when the runtime gave no reset, failover on,
  up to three), and never the dispatcher's secret for that variable. P's credential allowlist does not filter P's own
  accounts, and a failover moves only between P's accounts.
- **Every one of them is paused, cooling or past its end**: the run does not fall back to the dispatcher's secret. It
  does as it does when all of a member's accounts for a variable are unusable: with every account cooling it waits on
  the one ready first, with every one paused or ended it is leased none for that variable, and an event of the run says
  why, naming accounts by id.
- **P has no account for the variable on W** (or none on W at all): the run is leased the dispatcher's secret for it
  through the credential axis, as before.

Picking by hand, at dispatch or rerun, an account for a variable P has accounts for is 422 naming `@P`. The lease is
named `@<handle>:<name>`, which no secret of a member can be, so an answer never holds two leases of one name; the
`credential.lease` audit row names it by id and handle (`agent-accounts=12@<handle>`) apart from the dispatcher's
`secrets=`, and `GET .../runs/{id}/credentials`, `evo-agents hub run credentials` and the run's page show it under that
name. The JSON of a lease the worker gets is unchanged, so a worker of 0.24.x leases it without an upgrade. The hub
replaces the value of each account of the agent by `***` in the messages the agent posts to a chat, as the daemon does
in events.

Who sees what: every member of the project sees an account's name, variable, workers, state, weight, priority and its
usage, never the value; only an admin of the project adds, renews, changes and deletes it. A member reads them with
`GET /v1/projects/{p}/agents/{handle}/accounts[/{name}]`, or across every project they hold a grant on with
`GET /v1/agent-accounts` (the /accounts page), and with `evo-agents hub agent account list|show`
(`evo_agents/hub/server/agent_account_views.py`): provider, category, state, workers, weight, priority, end, last
usage, runs and cost over 24 hours and 7 days (null for a service account), the runs holding it and when it was last
leased, computed by the functions of a member's own accounts (`account_views`). Without a grant it is the 404 or 403
of reading the profile; `GET /v1/accounts` stays the caller's own accounts. Who can read the value, since
a run that holds it runs the agent's code: an admin, through the instructions of the profile (the agent runs under
them); a writer, through the agent of any run they dispatch; the owner of the worker and a root user on it, for every
account bound to it; and every other run on that worker, as for any lease (see "What is still a risk").

### Provider accounts

A **provider account** is an API key of a model provider with the endpoint it calls and the models it serves
(`evo_agents/hub/server/provider_accounts.py`, migration 0151). It is a secret of kind `provider`, whose sealed value is
the key, plus a row of `provider_accounts`: `provider`, the id of an entry of the model catalog; `base_url`, an https
root; `api_format` (`openai`, `anthropic`, `openai-responses` or `gemini`); and `models`, the names a run may ask for.
The catalog (`GET /v1/model-catalog`, `evo-agents hub model-catalog`) is generated from OmniRoute's provider registry at
the version the gateway image pins (`evo_agents/hub/model_catalog.py`, `GATEWAY_VERSION`). It lists each chat provider
an API key reaches, plus `custom-openai` and `custom-anthropic` for an endpoint the member names, and `claude`, whose
accounts keep the sign-in of the section above.

- A member writes theirs under `/v1/provider-accounts`, through `evo-agents hub provider-account` (`add`, `set`,
  `pause`, `resume`, `show`, `list`, `delete`, `test`, `models`) or the Providers tab of `/accounts`. PUT creates or replaces an account whole. PATCH changes what it is given, and
  needs no key, so a model or a binding changes without the key being typed again. A new key keeps the
  account's id, so its weight, priority, rotation, runs and shares go on. `from_secret` takes the key of one of the
  member's env secrets (`zhipu-glm` for `ZHIPU_API_KEY`): the hub opens it and seals it again under the new account,
  and the env secret stays as it was. An admin of a project writes an agent's provider accounts under
  `/v1/projects/{project}/agents/{handle}/provider-accounts`, by value only.
- The hub keeps a base URL only when every address its host resolves to is public. It refuses loopback, private,
  link-local, CGNAT, metadata, multicast and ULA addresses, single-label names and names under private suffixes, and
  checks them again at each call. Test connection and Fetch models (`POST .../probe`) call the endpoint from the hub,
  pinned to the address it checked, with the host in Host and SNI.
- The key is never leased: kind `provider` is not in `LEASED_KINDS`, and no route, log line, audit row, event or error
  holds it. `/v1/secrets` does not list it.
- A provider account is an account like the others: bound to projects and workers, with a weight, a priority, a pause,
  a cooldown and an end, shared through `/v1/secrets/{name}/shares`, picked by the rules of the section Accounts.

**The gateway.** The model calls of such accounts go through OmniRoute, the hub's `gateway` service: it runs on a
network only the api joins, with no port and no domain, and the hub reconciles its connections from the provider
accounts (`server/gateway_sync.py`; `docs/hub.md`, section Model gateway). It holds the keys, opened by the hub, so a
worker never does.

**A run on a provider's model.** A run whose model reads `<alias>/<model>`, with the catalog's alias of a provider
account's provider (or `evo-<id>` for a custom endpoint), is a gateway run (`evo_agents/hub/gateway_runs.py`,
migration 0152), on Claude Code, opencode or Codex. Only a daemon of `GATEWAY_RUN_AGENT` (0.46.0) or later claims it.
When the worker asks for the run's credentials, the hub has the gateway make a key for that run alone
(`server/gateway_leases.py`): it reaches only the connections of the accounts the run may use, ends 300 seconds after
the run's timeout would, and logs no request. The hub keeps that key sealed under the lease's id. The worker gets one
lease of kind `gateway`: the hub's route `<hub>/v1`, the **run token** `evr_...`, the model and the API format of the
runtime. The run token is an HMAC of the lease's id under the run's key. The hub stores only its SHA-256, and the
daemon masks it as it does every lease. The route (`/v1/messages`, `/v1/chat/completions`, `/v1/responses`,
`/v1/models`, `server/gateway_proxy.py`) takes the token, refuses it once the run leaves its held states or the lease is
revoked, and forwards the call with the run's key in its place. On Claude Code, `ANTHROPIC_BASE_URL` is the hub,
`ANTHROPIC_AUTH_TOKEN` the token, and the run's own config dir holds no Claude login. opencode and Codex read the
token from `EVO_GATEWAY_TOKEN` (`evo_agents/worker/runtimes/gateway.py`). When the run ends, the sweep reads the key's
usage into the run's figures, cost included, and revokes the key.

### Compared with a proxy

CLIProxyAPIPlus (github.com/kaitranntt/CLIProxyAPIPlus) solves a near problem another way: a process on the machine
that the CLI reaches through `ANTHROPIC_BASE_URL`, which logs in to each account with OAuth, keeps its refresh token,
and picks an account for each request (`routing.strategy` round-robin or fill-first, with weights), retries a request on
another account when one answers with its quota spent (`request-retry`), cools that account down, keeps a session on
one account while it can (`session-affinity`), and has a management panel. The hub now does both, by credential:

- **A Claude subscription goes straight to Anthropic.** A Claude account is the token `claude setup-token` makes,
  which the hub leases as it is, or an OAuth account (`account oauth-login`), whose tokens the hub keeps sealed and
  refreshes when a run asks for its credentials. Nothing sits between the CLI and Anthropic: no request rewritten, no
  identity changed, and the CLI's own `rate_limit_event` says when an account is spent. The hub applies the proxy's
  rules (round-robin, weighted, fill-first, cooldown, failover) per run, not per request: a run keeps one account for
  its whole life and moves only when that account hits its limit, so the session's prompt cache, usage figures and
  cost stay with one account. The value stays a lease, held in the worker's memory for the run that needs it and
  revoked when the run ends.
- **A provider account with an API key goes through the gateway.** These are the keys a proxy is made for: one call
  shape per format, many providers, limits that differ by provider. Here the proxy is the hub's own gateway, not a
  process on each worker. It holds the keys, the worker holds only a run token that dies with the run, and it may move
  a request between the accounts of the same provider the run may use (a run never moves between providers). The
  price is the one the first point avoids: the gateway, and the hub's route in front of it, see each prompt and answer
  of such runs (section What is still a risk).

What the proxy does that the hub does not, for a Claude account: it moves a request, not a run, so an agent in a
terminal pane goes on by itself. Here an interactive pane only shows the event, and only Claude Code reports a limit;
the usage an account shows is what the CLI said last.

The proxy's `--claude-login` is a PKCE login on claude.ai with Claude Code's client id, whose access and refresh
tokens the proxy keeps and refreshes. `hub account oauth-login` does the same flow on the member's machine, and the hub
keeps the sealed tokens and refreshes them at lease time (above); `hub account login`, with the token `claude
setup-token` prints, is unchanged and needs no refresh token. Anthropic's
[Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) page, section "Authentication and
credential use" (read 2026-10-14), does not permit third-party developers to offer Claude.ai login in their own
applications, and says developers may not collect, store or intermediate Claude.ai credentials or session tokens.
[Unverified] Whether `oauth-login` fits those terms is not settled by this document; its owner checks a use of an
account against that page, and `account login` with a setup-token is the route that keeps the sign-in inside
Anthropic's own binary.

### Sharing a secret or an account

The owner of a secret or an account may share it with one member, a unit of the org chart and every unit below it,
or everyone on the hub, for some of the projects it is bound to (`POST /v1/secrets/{name}/shares`, table
`credential_shares` of migration 0105):

```
evo-agents hub account share claude-a --login someone-else --project demo --level A2 --budget-usd 50 \
    --usage-ceiling 80 --max-running 2 --expires 2027-01-31 --ack-terms
evo-agents hub secret share notes-key --unit lab --project demo
evo-agents hub account unshare --share 12
evo-agents hub secret unshare --share 13
```

Only the owner creates a share, a hub admin included. The owner revokes it (`DELETE /v1/shares/{id}`), and a hub admin
may revoke any share; a revoked share stays, with who revoked it and when, and deleting the secret revokes its shares.
A forge secret is never shared. A unit grantee is a live unit: neither it nor a unit above it is archived, and no
cohort holding it has ended. `hub secret list --json` and `hub account show --json` carry each one's live shares under
`shares`; `GET /v1/secrets/{name}/shares` lists revoked ones too.

The hub sorts each credential into a class: `subscription` for an account of provider claude, whether its token was
pasted or came from OAuth; `api_key` for an Anthropic or OpenAI API key; `git`; and `env` for the rest. A subscription
token is shared only with the owner's acknowledgement of this warning (`terms_ack` in the API, `--ack-terms` on the
command line), which the web, the command line and the audit show word for word
(`evo_agents.hub.credential_shares.TERMS_WARNING`):

> This is a personal Claude subscription token. Anthropic's Consumer Terms (section 2) do not allow you to let anyone
> else use your account. Anthropic may limit, suspend or close the account without notice. Every request a run makes
> with this token is made in your name, as the account holder.

Without the acknowledgement the hub answers 409 with that warning, and the command prints it and exits non-zero. With
it, the share keeps `terms_ack_at`, and the audit gets a row `share.terms_ack` holding the warning after the row
`share.create`. Every write adds a `share.` audit row that names the secret by id, never by value.

#### Leasing under a share

A run's principal is the person it acts for (`runs.principal_id`, `docs/autonomy.md`), the member who dispatched it
for a run queued before schema 0113; each lease records it (`credential_leases.principal_id`, migration 0106). A share
covers a run when it is not revoked, has not ended, lists the run's project, and its grantee is the
principal, a live unit holding the principal's live primary seat or a unit above it, or everyone. The shared secret
must still be bound to the project; a run of the Curator and a release run never take one
(`evo_agents/hub/server/share_leases.py`).

The principal's own secrets come first: a shared account is taken only when none of their own for the variable is
ready (`run_accounts.how` is `shared`), and a shared git secret only for an origin none of their own covers. A share
lends nothing when its owner paused the account or it cools down (the owner's runs go first and may still take it
cooling), when the highest usage among the windows of its last report whose reset has not passed reaches
`usage_ceiling_pct` (unknown usage lends; the CLI reports a window's usage as a fraction of its limit, which the
hub reads as a whole percent, a fraction above 1 as 100), when `max_running` runs hold it, or when this month's cost (UTC) of the
runs under it plus an estimate for each run still running without a cost and for this one exceeds `budget_usd`. The
estimate is the mean cost of the share's runs this month, else 1.00 USD, never under 0.01 USD. A run that holds a
lease under a share keeps it. An agent's account (`hub agent account`) has no member owner, so no share names it; an
agent's own account for a variable comes before any share. When a refused share leaves the run with nothing for a variable or origin, a `system`
event of the run says why, naming the share by id alone.

Revoking a share ends the leases out under it at once; a share that ended or no longer covers a run ends them at the
run's next ask and on every pass of the reaper. `hub account show NAME --json` carries `cost_by_principal`: per
member, runs and cost over 24 hours, 7 days and this month. The owner sees every row; a member it was shared with
reads it with `--owner LOGIN` (`GET /v1/accounts/{name}?owner=LOGIN`) and sees their own row, runs and holders alone.
The share's `level` is source 6 of the autonomy levels (`docs/autonomy.md`): from the lease that lends it, a run
under the share acts at most at that level, the lowest level among its shares when it holds several
(`run_autonomy.share_levels`); a share without a level limits nothing, and a level never raises a run.

## Plugins

A plugin holds two kinds of credential, and neither reaches a worker ([plugins.md](plugins.md)).

- **Its token.** The hub mints a backend's token only when the plugin host is about to start it
  (`POST /internal/plugin-host/plugins/{name}/token`, over the host's shared secret): `evp_` and 32 random bytes in
  base64url, of which Postgres keeps only the SHA-256 (`plugin_tokens`, schema 0036). Minting revokes the plugin's
  older tokens, so a token lives as long as the backend it was made for; removing the plugin or the hub turning it off
  revokes them at once. It never signs in: only the plugin API under `/v1/plugin-api/` takes it, for what the plugin's
  installed version asks for and the project granted (`evo_agents/hub/server/plugin_tokens.py`).
- **The secret fields of its config.** A field its schema marks `"x-secret": true` is sealed with
  `EVO_HUB_SECRETS_KEY`, bound to the plugin, the layer, the project and the agent profile and the field's name
  (`plugin_config_aad`), and never shown again: answers name it as set, and the digest of an effective config counts
  it by a fingerprint of its sealed value. Without the key, setting one is a 503. The hub opens them only for the
  plugin host's pull of its own plugin's payload, which reaches the backend in `initialize` on its stdin, never in its
  environment or arguments; a hub without the key leaves the field out and names it in `missing_secrets`. The audit
  row `plugin.config` names the fields that changed, never a value. A worker gets neither: a run records only the
  version and digest of each plugin's effective config (`run_plugins`).

## What a run got

`GET /v1/projects/{project}/runs/{id}/credentials` lists every lease of the run, given back or not, oldest first:
`id`, `name` (the secret's, or `github-app:<account>`), `provider`, `kind`, `target` (the variable it set, or the
origins of the run's repos it answered for, space-separated, in the form of `normalize_origin`), `worker`, `issued_at`,
`expires_at` and `revoked_at`. It never carries a value, sealed or not. The run must be one the caller may read, and
only the member who dispatched it gets the list: another member gets 403, a hub admin included, since the leases name
the owner's secrets as `GET /v1/secrets` does to the owner alone. `evo-agents hub run credentials RUN [--json]` prints
it, each lease as out, expired, or revoked with the time, and the run's page on the web shows it to its owner in a
Credentials card.

## Life of a lease

1. **Ask.** The daemon asks right after its claim, before it prepares worktrees, so fetch already uses the lease.
2. **Refresh.** A GitHub token with less than `GITHUB_TOKEN_REFRESH_SECONDS` (10 minutes) left, or past its end, is
   asked for again before git gets it, so a fetch or push never starts with a token about to end; while the hub does
   not answer, git gets the token it has. A push of the daemon that the origin refuses for its credential (git says
   the authentication failed, or had no credential to send), on an origin a `git` lease covers, makes the daemon give
   the run's leases back and take them again once, which revokes the GitHub tokens held and makes new ones even when
   they had time left, and push once more; a second refusal fails the run, with no further try. A refused push of a
   repo in `missing` takes the leases again the same way, since its project may list it by now, and pushes once more
   only when a lease covers the repo then; otherwise the run fails with git's error and "the hub leased no credential
   for {repo}: {reason}". Runs of a plan may last 24 hours.
3. **Give back.** When the run ends, is parked, or the daemon stops, the daemon calls `DELETE` on the same route,
   whatever state the run is in by then (404 only for a run this worker never held). The hub marks the leases
   revoked, revokes each GitHub token (`DELETE /installation/token`), drops its sealed value, audits
   `credential.revoke` and answers `{revoked}`, how many leases were still out.
4. **Revoked anyway.** The hub does the same itself, as its own audited action, when a run leaves the held states (it
   ends, waits in review, or is parked; `run_state.move_run`), when the reaper ends a run whose worker stopped
   extending its lease, when the owner revokes the worker or its token, which takes every lease of the worker still
   out, and when an admin deletes a member's grant on a project or lowers it to reader, which takes every lease still
   out of the runs that member dispatched in the project (the audit row says `by=grant-deleted` or
   `by=grant-reader`). The leases are marked in the transaction of the move; the GitHub tokens are revoked once it commits, by
   the route that made the move and by every pass of the reaper, which also takes the tokens GitHub failed to answer
   for. The daily job that prunes run events drops the sealed GitHub tokens of leases past their end.

## How the daemon hands leases over

- **Memory only.** Leases live in the daemon's memory (`evo_agents/worker/credentials.py`), never in a file, a log
  line or an event: worker.log masks every lease value, and so does the filter each event of the run goes through
  before the spool. The daemon counts which of its runs hold each value: a run that gives its leases back drops its
  values from both, those it took again and replaced included, and a value another run still holds stays masked. So
  the daemon's memory holds the values of the runs in progress, not every value it was handed since it started.
- **A socket per run.** The daemon listens on `runs/<id>/cred.sock` (mode 0600, in the run's directory, mode 0700) while
  the run holds leases, and answers only processes of its own uid (`SO_PEERCRED` on Linux, `getpeereid` on macOS):
  one JSON request a connection, git's credential for a URL or the variables of the agent's environment.
- **git.** The env of the run's git (the daemon's fetch and push) and of its agent carries `GIT_CONFIG_COUNT` entries,
  after any the daemon's own environment sets: for each origin of the run's checkouts that a `git` lease covers and
  whose repo is not in `missing`, keyed by its https URL (an https origin as it is, an SSH or plain http one as the
  same repo over https), an empty
  `credential.{url}.helper` (which drops the machine's own helpers, such as osxkeychain or `gh auth git-credential`,
  for that URL) followed by `!evo-agents worker git-credential --run N` (this evo-agents by absolute path),
  `credential.{url}.useHttpPath=true`, so the helper learns which repo git asks for and picks the lease with the
  longest `url_prefix` covering it, and, when the origin is SSH or plain http, `url.{https url}.insteadOf={origin}`.
  The hub compares origins in their https form, so a lease covers an http origin too; the rewrite makes the run reach
  it over https, and the socket answers protocol `https` alone, so a lease never goes over plain http. Origins no
  lease covers keep the machine's helpers, and the machine's git config is not touched. `git-credential` answers `get`
  from the socket and does nothing on `store` and `erase`; once the run gave its leases back it answers nothing, so git
  fails to authenticate.
- **Environment.** `env` leases go into the agent's environment. An interactive pane does not write them into its
  script; it runs `eval "$(evo-agents worker env --run N)"`, which asks the socket and prints them as `export` lines.
  The socket of a run of the Curator answers that ask only with the ticket of a pane the daemon opened for a takeover
  (`EVO_ENV_TICKET`, set on that one command of the pane's script, good for one answer within 2 minutes), so code the
  run does not trust, such as a verify command or a hidden check of a judge run, gets none of its `env` leases, a
  subscription token among them, from the socket.
- **A container.** A run whose agent runs in a container of a plugin's environment (`docs/workers.md`, Environments
  of plugins) gets its `env` leases there and none of the daemon's own environment: the container provider passes
  each variable by name, so the engine's client reads the value and no command line holds it. Its `git` leases stay
  out of the container, since the run's git configuration names a helper of the worker that the container does not
  have: the daemon fetches and pushes the run's repos on the host with them, as for any run. The runtime's login in a
  container is therefore a lease (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` for Claude Code, a provider's key
  for opencode or Codex); the machine's own login, its keychain and its `~/.claude` are not there.
- **No lease value in a push.** Before every push the daemon reads the commits the push would send and refuses one
  whose message or added lines hold a value of the run's leases, naming the commit alone (floor S3, `lease_leaks.py`);
  a push from an agent's command asks the run's socket (`op leaks`), which answers the commit, never the value.
- **Umask.** The service starts the daemon, and so its agents, with umask 077.
- **No lease, machine's own.** An origin in `missing` gets a system event "no leased credential for {origin}:
  {reason}; git uses this machine's own", and git falls back to the machine's credentials, as before 0.5.0. A
  laptop whose owner set no secret runs as it always did. That holds even when a lease's `url_prefix` covers the
  origin: `github-app:<owner>` is for `https://github.com/<owner>`, but GitHub made its token for the repos the hub
  leased it for alone, so handed for another repo of the owner, one the project does not list say, it gets 403. The
  run's preflight then asks the remote with what the run holds, before anything is fetched: a repo in `missing`
  without an origin fails the run with the cause `origin`, and one git cannot read, or push to when the run pushes it,
  with `credentials` (`docs/workers.md`, "Preflight and failure causes").

With a `CLAUDE_CODE_OAUTH_TOKEN` lease the Claude Code adapter drops the daemon's `ANTHROPIC_API_KEY` from the agent's
environment, unless a lease sets the key too (the API key comes first in Claude Code's order of authentication), and
the interactive UI starts without `--remote-control`, which a `claude setup-token` token cannot open. A `system`
event of the run says each. `ANTHROPIC_AUTH_TOKEN` and an `apiKeyHelper` of the machine's settings also come before
the token in that order, and are left as they are.

## The agent and the hub

The agent reaches the hub's MCP through `evo-agents hub mcp`. Inside a run (`EVO_RUN_ID` set and a worker token on
the machine) it sends the worker token with `X-Evo-Run`, and the hub gives it a principal scoped to the run's
project, at most writer, never admin, at most the level its owner's grant allows. Once the run ends the worker token
no longer opens `/mcp` for it. `evo-agents kg serve` (the evo-kg plugin's server, which serves the `kg_*` tools of a
session that has it) does the same inside a run: with `--backend auto` or `hub` it asks the hub's `/mcp` with the
worker token and `X-Evo-Run`, so the run reads its project's graph on the hub whether or not the machine is signed in
or knows the worktree's project. A worker machine needs no machine token at all. The scope is the run the request
names, not a token of the agent's own: the agent holds the worker's token as its daemon does (see "The agent can act
as its worker" below). A run that runs as its owner also reaches the hub's REST API as them (next section); its scope
on `/mcp` stays the one above.

## A run that runs as its owner

The owner of a run of a step, a plan run or an author run can let its agent call the hub's REST API as them, with
their grants on every project and hub admin when they are one, but never their secrets, tokens or workers. A run of
the Curator, a release run, a chat run and the hub's other kinds never run as their owner. The agent then needs no
login on the worker: `evo-agents hub ...` in the run signs in as the owner with a token of the run's own.

**Choosing it.** The owner chooses per run, never for a whole project:

```sh
evo-agents hub run dispatch rollout 5 --as-me                 # the step's run runs as you
evo-agents hub run plan rollout --as-me                       # the plan run runs as you
evo-agents hub run access 41 --as-me                          # a run already queued or held: let it from now on
evo-agents hub run access 41 --revoke                         # stop it: its token ends at once
```

`--as-me` sends `as_me: true` with `POST /v1/projects/{p}/runs` or `.../plan-runs`; `hub run access` is `PUT
/v1/projects/{p}/runs/{id}/access` with `{"as_me": true|false}`, which only the member who dispatched the run may call,
on a run that has not ended (409 otherwise). A rerun keeps the choice of the run it reruns unless its body names
`as_me`, and the run that resumes a parked run and the next attempt of a lost one keep it too. `hub run show` says
"it runs as them on the hub's API" after who dispatched it, and its `--json`, like every Run the hub answers, carries
`as_owner`.

**The token.** While a worker holds such a run, the hub adds one lease to its answer to the worker's ask for
credentials: kind `hub`, provider `run-access`, named `run-access:<owner>` (as `hub run credentials` lists it), its
value the run's token `evr_...`. It is a token of kind `run` of the owner, `run:<id>` as its host in `hub token list`,
stored as a SHA-256 like every token and sealed in the lease, so a second ask hands the same token back. It
authenticates only while its lease is out, the run is held by that worker and still let, the owner still holds writer
on the run's project and the worker is not revoked: the run ending, being parked or given back, the reaper, the
owner's `--revoke`, `hub token revoke` of that token, the lost grant or the revoked worker end it at once, and every
later call gets 401. A run that resumes gets a new token at its first ask.

**What it never reaches.** Each path of these groups answers 403 with the reason (`run_access_rules.py`), whatever
the method, and a route added later under one of their prefixes is refused with the group:

- secrets and everything that holds a credential's value: `/v1/secrets`, `/v1/shares`, the accounts and provider
  accounts, an agent's accounts, browser identities, a run's credentials and accounts;
- tokens, invite links and signing in: `/v1/tokens`, `/v1/admin/tokens`, the invites, `/v1/auth/*` but `whoami`, a
  project's signal ingest keys;
- workers with their pools, the terminal, the live view, takeover and handback of a run;
- the access of any run, its own included, so no run grants running as its owner onward.

These groups refuse writes (POST, PUT, PATCH, DELETE) and leave reads open:

- grants: `/v1/admin/projects/{p}/grants/*` and `/v1/admin/users/*`, an access that would outlive the run;
- the owner's autonomy: `/v1/autonomy*`, the whole org chart `/v1/org*` (seats, units, ceilings), a project's policy,
  its agent profiles (`/v1/projects/{p}/agents*`) and agent types;
- runs: a dispatch (`POST /v1/projects/{p}/runs`), a plan run, an author run, a rerun, a message to a run or to a
  local run's inbox, a release run or schedule, an editorial post's run. No run starts from it, with or without
  `as_me`: the owner's account would go on into work the owner never chose, with the accounts, browser identities or
  worker the request names;
- decisions: the answer to a decision of any run, a local run's decision marked answered, the approval of a run in
  review, the merge queue's answers, the reset of the Loop's breaker, the answers and rollbacks of `@builder`;
- the Curator: its charter, proposals, changes, probes and builds.

Behind the groups, every rule of the form "the agent of a run never does X" takes the run's token as it takes the agent
of a run on `/mcp` (`Principal.acts_for_run`): it writes no goal, opportunity or problem, answers no decision, approves
or returns no SRS version, decides no item of a canvas baseline, claims no post of a marketing channel's browser
session, whose answer carries the session's cookies (`session_run` of the core plugin API), reads no probe's verify,
uses no invite's right to register a project, and the K8 and K12 classes of its run's autonomy hold its writes
(`route_autonomy`). And no run starts while a request of its token is answered (`run_access_rules.ACTING_RUN`), whatever
route it took: a message it posts in a chat stays, and its mention of an agent is refused in the thread, with no run
and nothing in a run's inbox; a core plugin's task is not handed. The rerun the agent of a triage thread makes never
runs as the owner, whatever the failed run did. A stream of a run's log or of a chat the token opened looks at each
turn, at most every 5 seconds, whether the token is still live, and stops once it is not.

What stays open is the owner's ordinary work: plans and their steps, memories, chats, their messages, labels and
people (`hub chat label`), Telegram's webhook (`hub admin telegram`), a project's registration and sinks, plugins and
reading everything the owner reads. Each write made with it, refused or not, adds the audit row `run_access.write`
naming the run, the method, the path and the status; `run_access.grant` and `run_access.revoke` record each change of
the choice, and the `credential.lease` row names the token by id. No log line, event, audit row or error holds its
value.

**How the daemon hands it over.** The daemon puts the token into the agent's environment as `EVO_HUB_TOKEN` and the
hub's URL as `EVO_HUB_URL`: the URL the daemon itself reaches the hub at, the one the token was made through, else the
lease's own; `https`, or `http` to a loopback address, as for any hub URL (`run_access_env` in
`evo_agents/worker/credentials.py`). The value is masked in worker.log and in the run's events like every lease value,
and an interactive pane gets both variables from `eval "$(evo-agents worker env --run N)"`, never in its script. That
lease is the one way the two variables get there: an `env` secret may not name a variable starting with `EVO_`
(`env_name_refusal`), the daemon drops the ones its own environment holds from every run's agent, and a run of the
Curator or a release run gets none even if a lease came. A daemon older than this release keeps the lease, masks it and
puts nothing of it into the agent's environment; the run then goes on as one not let.

**The command line in the run.** `evo-agents hub ...` takes `EVO_HUB_TOKEN` and `EVO_HUB_URL`, when both are set and
neither is blank, before the login of `~/.evo/hub` and of `EVO_HUB_HOME` (`env_credentials` in
`evo_agents/hub/client.py`), so `evo-agents hub whoami` in the run prints the owner's login and `token N: run on
run:<id>`. One of the two alone changes nothing. `hub logout` refuses while they are set and deletes no file; `hub
login` still saves its login, which commands use once the two are unset. The SessionStart hook of the run's agent reads
the owner's memories with them. `evo-agents hub mcp` and `evo-agents kg serve` never send the run's token: inside a
run they keep the worker token and the scope of "The agent and the hub", and fall back to the login on disk, as
before.

**The risk.** For as long as the run holds the token, whatever steers its agent acts as the owner on the hub, within
the groups above: a prompt injected through a page, an issue or a file the agent reads can read every project the
owner reads, and change plans, memories, chats and their labels, a project's sinks and plugins, and, when the owner is a
hub admin, what a hub admin changes outside the groups (features, branding, market sources). It cannot start a run,
answer a decision, change a grant or anyone's autonomy, nor reach a secret or a token. A chat label it changes moves
which members read a chat, and a sink it changes moves where a project's messages may go: both are written in the audit
as `run_access.write`. Let a run run as you only when its work needs your account, and cut it with `evo-agents hub run
access RUN --revoke`, `evo-agents hub run cancel RUN`, or `evo-agents hub token revoke ID` of the token `hub token list`
shows with host `run:<id>`; the audit rows `run_access.write` say what it wrote.

## A run that asks for a credential

A run that lacks a credential asks its owner for it with a decision, rather than for a login on the worker's machine
or a value in `evo cred` there; the plan run's prompt tells its agent so. The request names a variable of the agent's
environment the run needs, such as an API key, the owner's account on the hub, or both in one decision:

```sh
evo-agents worker ask --category credential --question "The deploy needs its key and the hub." \
  --env-var NORTHWIND_DEPLOY_KEY --secret-name northwind-deploy --hub-account --step 3
```

The request goes with the decision as `credential_request` (`{env_var, secret_name, hub_account}`,
`evo_agents/hub/credential_requests.py`), and `--secret-name` defaults to the variable in lower case with `-` for `_`.
The hub refuses a variable no secret may set (`EVO_`, `GIT_`, `LD_`, `DYLD_`, `PYTHON...`, `PATH`, `HOME` and the
others of `env_name_refusal`), and gives the decision its own options, never the agent's: `value` (paste the value),
`as_me` (let the run use my hub account), `both`, and `refuse`, as the request asks. It is asked of the run's owner
whoever the run's principal reports to, since what it asks for is the owner's alone to give.

**Answering it.** The owner pastes the value, never on a command line:

```sh
evo-agents hub decision show 91                                # what it asks for, and the secret it would fill
pbpaste | evo-agents hub decision answer 91 --secret           # option value: the value from stdin
evo-agents hub decision answer 91 --secret --option both       # asked for without echo in a terminal, and as_me
evo-agents hub decision answer 91 --option as_me               # the hub account alone
evo-agents hub decision answer 91 --option refuse              # nothing
```

`--secret` sends the value as `secret` in `POST /v1/projects/{p}/decisions/{id}/answer`, which the web's password
field sends too; a value with no option stands for `value`. The hub keeps it as the owner's secret of kind `env` for
that variable, under the rules of `PUT /v1/secrets` (at most 16 KiB, no NUL, at most 200 secrets, the hub's key; 503
without it): the owner's live env secret for that variable bound to the run's project has its value replaced, the one
of the proposed name first, so no second one is made; with none, the secret of the proposed name is made, bound to the
run's project and, when the run is on a worker of the owner's, to that worker. A proposed name the owner holds for
something else is 409 and changes nothing. `as_me` and `both` let the run run as its owner as `hub run access --as-me`
does (a run of the Curator or a release run never does; 409), with the audit row `run_access.grant` ending
`by=decision:<id>`. Only the member who dispatched the run gives either (403 for a lead the decision escalated to, who
may still refuse); a run's token answers no decision at all.

The decision's answer carries a sentence of the hub's after the owner's words: which secret keeps the value, by name,
and whether the run now runs as its owner, never the value. The value is in no decision, answer, inbox message, event,
log line, audit row or error: the audit row `secret.put` names the secret by id and the decision (`secret:<id>
decision:<id>`), a 422 never echoes a request's input, and an answer whose words hold the value it pastes is refused.

**Going on.** The run goes on without being dispatched again. A parked run is resumed by the answer, and the run that
resumes it is leased the new secret and, with `as_me`, a token of its own from its start. A run still waiting gets the
answer in its inbox; `worker ask` noted the decision as a credential request in the run's file of decisions, so the
daemon asks for the run's leases again before it hands the answer to the agent (`Agent.credentials_answered`). The
agent reads what came with `eval "$(evo-agents worker env --run "$EVO_RUN_ID")"` before the command that needs it,
and its next start has it in its environment; the run's log says the leases were taken again, never their values. A
worker older than this release takes nothing again: the run gets what was given when it is leased next, at its resume.

**The risk.** A pasted value is a secret like any other: every run of the owner's on that project is leased it, on that
worker or on any of theirs, until the owner deletes it (`evo-agents hub secret delete NAME`). The account on the hub
holds the risk of "A run that runs as its owner" above for as long as the run holds its token.

## Who may hand a worker its work

`dispatch_from` is a switch per worker: `any` (the default) or `web`. A worker set to `web` claims only runs
dispatched from a web session, so a machine token that leaked from some other machine cannot put a plan step onto it.
Only the worker's owner, signed in on the web, flips it.

- **The switch.** `POST /v1/workers/{id}/dispatch-from` with `{"value": "any"}` or `{"value": "web"}`, from the
  owner's web session and its `X-Evo-CSRF` header; the web shows it on the worker's page as "Only runs dispatched from
  the web". A machine token gets 403 there, whoever holds it and whichever worker it names, so a token that leaked
  cannot turn the switch back off; a hub admin who does not own the worker gets 403, another member 404, a revoked
  worker 409. Each change adds an audit row `worker.dispatch_from` whose target ends `dispatch_from=any` or
  `dispatch_from=web`; setting the value the worker has changes nothing.
- **What a run records.** `POST /v1/projects/{p}/runs`, `POST /v1/projects/{p}/plan-runs` and `.../runs/{id}/rerun`
  record in `runs.dispatched_via` the credential of the caller: `web` for a web session, `machine` for any token, the
  machine token of `evo-agents hub run dispatch` and the worker token an agent reaches `/mcp` with alike
  (`credentials.dispatch_credential`). The next attempt of a lost run and the run that resumes a parked plan run keep
  the value of the run they follow. A run the night shift of a project's charter queued records `schedule`
  (`docs/curator.md`). A run shows it as `dispatched_via`; runs dispatched before 0.5.0 have none.
- **What a worker set to `web` takes.** Its claims pass over every queued run whose `dispatched_via` is not `web`,
  those without one included, and leave them for the owner's other workers; so a charter cannot name such a worker as
  its worker on duty. A dispatch, plan run or rerun pinned to it
  with a token gets 403, "worker NAME takes only runs dispatched from a web session, as its owner set it, ...", and
  nothing is queued. Runs that were queued for it with a token before the switch was turned on stay queued until the
  owner cancels them or turns the switch off again.
- **Steering what it runs.** A token that cannot hand such a worker work cannot steer the work it has either. A
  message to a run's agent (`POST .../runs/{id}/messages`) and the answer to a decision (`POST
  .../decisions/{id}/answer`), which goes to the agent as a message and, for a parked plan run, queues the run that
  resumes it on the worker it was parked on, get 403 with a token when the run is held by, was parked on or is pinned
  to a worker set to `web`, or is queued without a pin, dispatched from the web, while one of the owner's workers is
  set to `web` and may claim it: "run N is held by worker NAME, which takes only runs dispatched from a web session,
  as its owner set it, so a token cannot send its agent a message: send it from the web; nothing was sent". Nothing
  is answered, sent or queued. The owner does both from the web (`runs.web_only_steering`).

## Checking a worker machine

`evo-agents worker doctor [--json]` (`evo_agents/worker/doctor.py`) reads the machine, changes nothing, and lists
what it still holds or allows that a worker should not. Each finding has a `code`, a `severity` (`high`, `medium` or
`low`), the `subject` it is about (an account, a path, a host, a runtime), a `detail` and a `fix`. The command exits 0
when no finding is high and 2 when one is. A check that cannot run, its tool missing from PATH for one, is listed
under `skipped` with the reason and changes no exit status. No secret is printed: a token or a key is named by its
file or keychain item.

| Code | Severity | What it finds |
| --- | --- | --- |
| `other-admin` | high | another account of the admin group: `dscl . read /Groups/admin GroupMembership` on macOS, the groups `sudo` and `wheel` on Linux; root and macOS's system accounts (`_name`) do not count |
| `home-readable` | high, medium, low | the home, `~/.evo` or `~/github` readable by group or other: high when every account reads it, medium when other members of its group do, low when only the mode of the home keeps them out |
| `machine-token-on-worker` | high, medium, low | `~/.evo/hub/token`: high when the hub's `GET /v1/auth/whoami` says its login is an admin of the hub, medium for a member or when the hub does not say, low when the hub refuses it as revoked or expired |
| `gh-token-file` | high | an `oauth_token` in gh's `hosts.yml` (`GH_CONFIG_DIR`, else `~/.config/gh`); the host and login are named, never the token |
| `ssh-private-key` | high | a private key under `~/.ssh` that opens without a passphrase (`ssh-keygen -y -P "" -f KEY`), or one ssh-keygen refuses because group or other can read it |
| `runtime-login-stored` | medium | Claude Code still signed in (the keychain item `Claude Code-credentials`, asked with `security find-generic-password` for its attributes only, or `~/.claude/.credentials.json`), Codex's `~/.codex/auth.json` |
| `path-not-owned` | high, medium | a directory on the service's PATH, a runtime's binary (`claude`, `opencode`, `codex`, `tmux`, `git`) or its directory, owned by an account other than the user and root, or writable by group or other; medium when only administrators can write it, since `other-admin` names them already |

The service's PATH is the one the installed LaunchAgent or systemd unit sets, or else PATH as `evo-agents worker
service install` would keep it. Asking the hub whose `~/.evo/hub/token` it is sends that token to the hub it came
from, as `evo-agents hub whoami` does. On a machine another administrator shares, `other-admin` stays high however
the rest is fixed: the design limits what that administrator gets to the leases of the runs in progress.

`evo-agents worker service install` runs the doctor with the PATH it writes into the service and prints its high and
medium findings as `warning:` lines; they never stop the install.

## What is still a risk

- **The gateway sees the prompts of a provider account's runs.** A run on a provider's model sends each request
  and answer through the hub's route and the gateway, in clear inside the hub: whoever runs the hub's host, or takes
  over its api or gateway container, reads them, and the keys the gateway holds. The gateway's keys log no request
  (`noLog`), the route logs no body, token or key, and the gateway has no port or domain. Still, those protections hold
  only as far as the host is trusted. A Claude subscription does not take this path. Keep a model whose prompts must
  not reach the hub on a Claude account or a worker's own `env` lease.
- Root on the worker reads the leases a run is using: the agent's environment, the daemon's memory, the run's
  socket. A GitHub token so read lasts at most an hour and covers only the run's repos.
- **The agent can act as its worker.** A run's agent runs as the daemon's user, with no sandbox, so it can read
  `~/.evo/worker/token`, and the socket of every run (`runs/<id>/cred.sock`) answers any process of that user. With
  them an agent, or an instruction injected into it, can:
  - get the leases of every other run the worker holds at the time, of any of its projects: `evo-agents worker env
    --run N` and `worker git-credential --run N` ask that run's socket (the socket of a run of the Curator answers
    neither without a ticket of the daemon's), and `POST /v1/worker/runs/{N}/credentials` asks the hub;
  - reach the hub's MCP as another run the worker holds (`EVO_RUN_ID=N evo-agents hub mcp`), in that run's project;
  - report, end or give back the runs of the worker, and claim a queued run of any project the worker serves (`POST
    /v1/worker/claim`) while a slot is free, then lease that run's credentials and read its prompt and plan.

  The hub cannot tell the agent from its daemon: both hold the same token. A token of the run's own, which the daemon
  would keep from the agent, was weighed and left out, since the agent runs as the daemon's user and reads what the
  daemon reads. What limits it: register a worker for one project only (one `--project` at `evo-agents worker
  register`, or one project in the pairing), so whatever its agent reaches is of the project it works in already;
  give it `--slots 1`, so no other run is held, and no other run's socket open, while one runs (the agent may still
  end its own run and claim the next queued one); and run a worker for each project of a different sensitivity
  under a user of its own (its own uid, home, `~/.evo/worker` and service), whose files and sockets the others cannot
  read.
- **A shared worker extends this to every member it is shared with** (`docs/workers.md`, Shared workers). The claim
  holds the runs of one member at a time, so no two members' runs are held side by side, but the agent of a member's
  run still reads the worker's token: it can end its own run and claim the next queued one, another member's, with
  its leases and prompt, and the token works from elsewhere until the worker is revoked. The machine's own
  credentials, and the worktrees of earlier runs, are the daemon user's for every member's run. Share a worker only
  among members who trust each other's runs with their leases, on a machine whose user holds no credential of its
  owner beyond the worker's token.
- A static secret (a GitLab project access token, a `claude setup-token` token) read during a run works until it
  expires on its own service: 90 days, a year. Keep one per project, with the least role (Developer), with an end
  date, bound to the workers that need it.
- `EVO_HUB_SECRETS_KEY` and a dump of the hub's database together open every secret. Keep the key only in the hub's
  environment and the operator's secret store; rotate it by `key_id` (see the harness's `docs/hub-deployment.md`).
- The GitHub App's private key lets whoever holds it ask a token for every repo the App is installed on.
- The Curator's runs share the worker's user with every other run, so its agent reads what the worker holds; the
  Curator's worker on duty therefore holds no credential of its owner but its leases, takes one run at a time
  (`--slots 1`), and the night shift never runs a Builder and the Judge of the same change at once. A git push the
  agent makes on its own with the owner's own credentials would get past the ruleset, since the owner bypasses it: keep
  none on that machine. The daemon pushes a Builder's branch only with a lease that covers its origin, and the agent of
  a run of the Curator never gets the lease: the run's socket answers only a git command of the daemon's own, with the
  ticket of that command, and gives the `env` leases only to a pane the daemon opened, with that pane's ticket, once.
  What a process of the same user can still reach, which a separate user, or a VM per worker, closes:
  - the ticket of a git command of the daemon, from that command's environment while it runs [Inference];
  - the ticket of a pane, from the pane's script (mode 0700, in the run's directory, which its first line deletes)
    between the takeover that writes it and the pane's start, or from the environment of the pane's one
    `evo-agents worker env`; a ticket taken first leaves the pane without the `env` leases;
  - the `env` leases in the agent's own environment while the agent runs, as the operating system shows a process's
    environment to its user (`/proc/<pid>/environ` on Linux). A judge run runs the code it judges before its agent
    starts and kills what each command left once it ends, so that code reads the agent's environment only if one of
    its processes outlives that kill;
  - the leases in the daemon's memory, for root, as above.
- A worker set to `web` takes no run, message or decision answer from a token, but a plan run reads its plan as the
  hub holds it now (`GET /v1/worker/runs/{id}/plan`), and a machine token of a writer of the project may still edit
  that plan (`PUT` or `PATCH .../plans/{plan}`, `evo-agents hub plan put` or `plan patch`, a step's status through
  `plan step` or the `plan_step` MCP tool). A step's text is what the agent of the plan run does next, so a token that
  leaked steers such a run through its plan. Plan edits are not gated by `dispatch_from`; every revision is kept with
  who wrote it (`evo-agents hub plan history`), so such an edit can be found afterwards, not prevented.

## When a credential may have leaked

| Credential | Cut it off |
| --- | --- |
| a worker | revoke the worker on the Workers page: its token stops, its runs end, the hub revokes their leases |
| a secret | delete it (`evo-agents hub secret delete NAME` or the Secrets page), then revoke it at its source |
| a GitLab access token | GitLab, the project's Settings > Access tokens > Revoke |
| a `claude setup-token` token | claude.ai, Settings > Claude Code, revoke the token |
| an agent account | delete it (`evo-agents hub agent account remove`) or the agent on the project's page, then revoke it at its source; a run that holds it keeps the value until it ends, so end those runs too |
| a GitHub App token | it ends within the hour; to end all at once, suspend or uninstall the App, or rotate its private key |
| `EVO_HUB_SECRETS_KEY` | rotate the key, then have every owner set their secrets again |

## Browser identities

A browser identity is a Chromium profile kept logged in for one worker (see [docs/browser.md](browser.md)). Each version
of it is sealed with a fresh AES-256-GCM data key, itself sealed under `EVO_HUB_SECRETS_KEY` with the identity and the
version as associated data; the hub keeps two versions, and a delete drops the keys first. An identity of an agent profile
is an admin's and is retired with the profile. No route returns a cookie, a key or a hash of a blob.
