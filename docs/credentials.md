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
client, `evo_agents/hub/server/credentials.py` the worker's lease route, and `evo_agents/worker/credentials.py` the
daemon's side.

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

- a run gets only credentials for its own project, its own repos and its own worker;
- the GitHub token covers only the run's repos, with `contents: write` and `metadata: read`, and lives an hour;
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
the owner holds writer on, and optionally to some of the owner's workers. A hub admin sees only their own secrets.

The **GitHub App** is the hub's own (`EVO_HUB_GITHUB_APP_ID`, `EVO_HUB_GITHUB_APP_PRIVATE_KEY`). For each owner of a
run's github.com repos the hub finds the installation (`GET /repos/{owner}/{repo}/installation`) and asks for a token
for those repos only (`POST /app/installations/{id}/access_tokens`). A run over repos of two GitHub owners gets two
tokens. A repo the App is not installed on gets none, and the run is told why: "the GitHub App is not installed on
owner/repo". The hub signs each of these calls with a JWT of the App (RS256, issued 60 seconds back, ending 9 minutes
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
  password, query or fragment, kept in the form of `normalize_origin`, and `username` (`oauth2` when left out). A field
  of the other kind is refused.
- `projects`: one or more projects the caller holds writer on; a project the caller holds less on is 403, one they
  cannot see 404.
- `workers`: optional names of the caller's own workers that are not revoked; any other name, another member's worker
  included, is 403, also for a hub admin. Left out, the secret goes to any worker of its owner. The bindings are each
  project on each named worker.
- `expires_at`: optional, with a time zone, in the future.
- `value`: 1 to `MAX_SECRET_BYTES` (16384) bytes of UTF-8, without NUL; one line for kind `git`.

A name is 1 to 64 characters of `a-z`, `0-9`, `.`, `_` and `-`, starting with a letter or digit, unique among the
owner's secrets that are not deleted; a deleted secret's name is free again. A member keeps at most
`MAX_SECRETS_PER_OWNER` (200) secrets (409 past that). A hub admin's routes are the same: they list, change and delete
only their own secrets, and `/v1/admin/stats` counts the rows of `secrets` without showing one. Each write adds an
audit row `secret.put` or `secret.delete` whose target is the secret's name; no audit row, log line or error carries
a value, and a 422 never repeats the input.

## Which leases a run gets

`evo_agents/hub/server/credentials.py`. `POST /v1/worker/runs/{id}/credentials`, with the worker's token and the
protocol header, for a run the worker holds: one of the held states, `waiting` of a plan run among them. Any other run,
another worker's or one that ended, is 404, as for a state report; a machine token or a web session gets 403, as on
every worker route. The hub takes the run's repos (the run's repo for a step run, the repos of the plan run, those of
the plan's steps not done when it was dispatched) and their origins from `project_repos`, compared in the form of
`normalize_origin` (`git@host:path` and `ssh://git@host[:port]/path` included). It then leases what the run's owner
bound to the run's project, bound to no worker or to this one, and not past its `expires_at`:

- each `env` secret, one per variable: a secret bound to this worker wins over one bound to any worker, then the
  first by name;
- each `git` secret whose `url_prefix` covers one of those origins, the longest prefix for each origin;
- a GitHub App token for the origins on github.com that no `git` secret covers, one per installation: a lease of kind
  `git` named `github-app:<owner>` for `https://github.com/<owner>`, with the username `x-access-token`.

The answer is `{leases, missing}`. Each lease carries `id`, `kind`, `provider`, `name`, `env_var`, `url_prefix`,
`username`, `value` and `expires_at`, as `Lease.from_json` reads them. `missing` names each repo whose origin nothing
covers, with its origin (null when the project registered none) and the reason: no origin, no `git` secret covering
it, "the GitHub App is not installed on owner/repo", no GitHub App configured, GitHub failing. Without
`EVO_HUB_SECRETS_KEY` nothing is leased and every repo is missing with that reason.

Each call records its leases in `credential_leases` and adds one audit row `credential.lease` naming the run, the
secrets, the App's accounts and the repos, never a value. A secret is leased once per run and worker: asked again,
the same lease comes back with the secret's value as it is now. A GitHub token stays sealed in its lease, bound to the
lease's id, until it expires, so the hub can revoke it. Asked again, a run gets the same tokens while each has
`GITHUB_TOKEN_REFRESH_SECONDS` or more left, and new ones in new leases after that; the tokens they replace go on
working until they expire, so a push that took one is not cut off. GitHub is asked between two transactions, never
while the run's row is locked; when the run ended in between, the tokens just made are revoked and the answer is 404.

## Life of a lease

1. **Ask.** The daemon asks right after its claim, before it prepares worktrees, so fetch already uses the lease.
2. **Refresh.** A GitHub token with less than `GITHUB_TOKEN_REFRESH_SECONDS` (10 minutes) left is asked for again
   before git gets it, so a fetch or push never starts with a token about to end; while the hub does not answer, git
   gets the token it has. Runs of a plan may last 24 hours.
3. **Give back.** When the run ends, is parked, or the daemon stops, the daemon calls `DELETE` on the same route,
   whatever state the run is in by then (404 only for a run this worker never held). The hub marks the leases
   revoked, revokes each GitHub token (`DELETE /installation/token`), drops its sealed value, audits
   `credential.revoke` and answers `{revoked}`, how many leases were still out.
4. **Revoked anyway.** The hub does the same itself, as its own audited action, when a run leaves the held states (it
   ends, waits in review, or is parked; `run_state.move_run`), when the reaper ends a run whose worker stopped
   extending its lease, and when the owner revokes the worker or its token, which takes every lease of the worker
   still out. The leases are marked in the transaction of the move; the GitHub tokens are revoked once it commits, by
   the route that made the move and by every pass of the reaper, which also takes the tokens GitHub failed to answer
   for. The daily job that prunes run events drops the sealed GitHub tokens of leases past their end.

## How the daemon hands leases over

- **Memory only.** Leases live in the daemon's memory (`evo_agents/worker/credentials.py`), never in a file, a log
  line or an event: worker.log masks every lease value, and so does the filter each event of the run goes through
  before the spool.
- **A socket per run.** The daemon listens on `runs/<id>/cred.sock` (mode 0600, in the run's directory, mode 0700) while
  the run holds leases, and answers only processes of its own uid (`SO_PEERCRED` on Linux, `getpeereid` on macOS):
  one JSON request a connection, git's credential for a URL or the variables of the agent's environment.
- **git.** The env of the run's git (the daemon's fetch and push) and of its agent carries `GIT_CONFIG_COUNT` entries,
  after any the daemon's own environment sets: for each origin of the run's checkouts that a `git` lease covers, keyed
  by its https URL (the origin itself when it is http or https), an empty `credential.{url}.helper` (which drops the
  machine's own helpers, such as osxkeychain or `gh auth git-credential`, for that URL) followed by `!evo-agents worker
  git-credential --run N` (this evo-agents by absolute path), `credential.{url}.useHttpPath=true`, so the helper
  learns which repo git asks for and picks the lease with the longest `url_prefix` covering it, and, when the origin
  is SSH, `url.{https url}.insteadOf={ssh origin}`. Origins no lease covers keep the machine's helpers, and the
  machine's git config is not touched. `git-credential` answers `get` from the socket and does nothing on `store` and
  `erase`; once the run gave its leases back it answers nothing, so git fails to authenticate.
- **Environment.** `env` leases go into the agent's environment. An interactive pane does not write them into its
  script; it runs `eval "$(evo-agents worker env --run N)"`, which asks the socket and prints them as `export` lines.
- **Umask.** The service starts the daemon, and so its agents, with umask 077.
- **No lease, machine's own.** An origin in `missing` gets a system event "no leased credential for {origin}:
  {reason}; git uses this machine's own", and git falls back to the machine's credentials, as before 0.5.0. A
  laptop whose owner set no secret runs as it always did.

With a `CLAUDE_CODE_OAUTH_TOKEN` lease the Claude Code adapter drops `ANTHROPIC_API_KEY` from the agent's environment
(the API key comes first in Claude Code's order of authentication), and the interactive UI starts without
`--remote-control`, which a `claude setup-token` token cannot open.

## The agent and the hub

The agent reaches the hub's MCP through `evo-agents hub mcp`. Inside a run (`EVO_RUN_ID` set and a worker token on
the machine) it sends the worker token with `X-Evo-Run`, and the hub gives it a principal scoped to the run's
project, at most writer, never admin, at most the level its owner's grant allows. Once the run ends the worker token
no longer opens `/mcp` for it. A worker machine needs no machine token at all.

## Who may hand a worker its work

`dispatch_from` is a switch per worker: `any` (the default) or `web`. A worker set to `web` claims only runs
dispatched from a web session, so a machine token that leaked from some other machine cannot put a plan step onto it.
Only the worker's owner, signed in on the web, flips it.

## What is still a risk

- Root on the worker reads the leases a run is using: the agent's environment, the daemon's memory, the run's
  socket. A GitHub token so read lasts at most an hour and covers only the run's repos.
- A static secret (a GitLab project access token, a `claude setup-token` token) read during a run works until it
  expires on its own service: 90 days, a year. Keep one per project, with the least role (Developer), with an end
  date, bound to the workers that need it.
- `EVO_HUB_SECRETS_KEY` and a dump of the hub's database together open every secret. Keep the key only in the hub's
  environment and the operator's secret store; rotate it by `key_id` (see the harness's `docs/hub-deployment.md`).
- The GitHub App's private key lets whoever holds it ask a token for every repo the App is installed on.

## When a credential may have leaked

| Credential | Cut it off |
| --- | --- |
| a worker | revoke the worker on the Workers page: its token stops, its runs end, the hub revokes their leases |
| a secret | delete it (`evo-agents hub secret delete NAME` or the Secrets page), then revoke it at its source |
| a GitLab access token | GitLab, the project's Settings > Access tokens > Revoke |
| a `claude setup-token` token | claude.ai, Settings > Claude Code, revoke the token |
| a GitHub App token | it ends within the hour; to end all at once, suspend or uninstall the App, or rotate its private key |
| `EVO_HUB_SECRETS_KEY` | rotate the key, then have every owner set their secrets again |
