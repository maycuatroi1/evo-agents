# Guard: private names out of public repos

The core plugin **guard** (`evo_agents/core_plugins/guard`) keeps the private names of a hub's projects out of the
repos they publish. A project lists its private names (its own name, hosts, domains, groups, repos) and the repos it
publishes; the hub hands every worker and member the union of both, and their guards refuse a commit message, a push,
or the text of a pull request, issue, comment or release bound for a published repo that holds one of the names.

A rule in an agent's instructions is read by the agents that load those instructions. The guard runs in every run of a
worker and in every Claude Code session with the evo-hub plugin, whatever instructions it loaded.

## The lists

A project enables the plugin like any other (`evo-agents hub plugin enable guard --project P`, or the project's Plugins
page). Its admins, and the hub's, keep both lists; its members read them:

```sh
evo-agents hub guard add-term "Northwind" --project northwind --note "the client's name"
evo-agents hub guard add-term "northwind-ops.example" --project northwind
evo-agents hub guard add-target maycuatroi1/evo-agents --project evo-agents
evo-agents hub guard add-target "github.com/acme/open-*" --project evo-agents
evo-agents hub guard terms --project northwind
evo-agents hub guard check notes.md --repo maycuatroi1/evo-agents --project evo-agents
```

| Route (under `/v1/x/guard/<project>/`) | Who | What |
| --- | --- | --- |
| `GET terms`, `GET targets` | a member of the project | the lists |
| `POST terms` `{term, note}`, `DELETE terms/{id}` | an admin of the project or of the hub | 409 for a term of the same words, 422 for one without a letter or digit |
| `POST targets` `{pattern, note}`, `DELETE targets/{id}` | an admin of the project or of the hub | a URL, `OWNER/REPO` (GitHub) or `host/owner/repo`, `*` and `?` as wildcards |
| `POST check` `{text, repo}` | a member of the project | the names the text holds, and whether the repo is guarded; nothing is kept |

Each change is one audit row (`guard-term.add`, `guard-term.remove`, `guard-target.add`, `guard-target.remove`) whose
target names the row's id, never the term. The tables are `guard_terms` and `guard_targets` (migration 0192).

## How a name matches

`evo_agents.private_names` splits a text into words (runs of letters and digits, lowercased after NFKC), so `-`, `_`,
`.` and spaces separate words. A term matches the same words in a row in any case: `northwind-ops` matches
`Northwind Ops`, `northwind_ops` and `NORTHWIND.OPS`. A term of one word of at least five characters also matches
inside a longer word (`NorthwindCLI`, `northwindharness`), and a term of several words also matches them written
together (`northwindops`).

## The policy

`GET /v1/guard/policy` (a member, or the agent of a run) and `GET /v1/worker/guard/policy` (a worker) answer the hub's
policy: the terms of every project that enables the plugin, each as `{h, n, part}` (a salted BLAKE2b of its words, how
many words, and the length of a term matched inside words), and the targets. No term is in clear, so the file a machine
keeps, a log line or an agent that reads it never spells one. `digest` is empty when there is no term or no target:
nothing is guarded then. The answer carries an `ETag` and takes `If-None-Match`.

## The guards

**Worker** (`evo_agents/worker/guard.py`). The heartbeat answer's `settings.guard` is the policy's digest. When it
differs from the policy the worker holds, the daemon fetches the policy into `<worker home>/guard/policy.json`, or
removes the file for an empty digest. Its environment names the file in `EVO_GUARD_POLICY` from its start, so:

- the daemon's own pushes (`gitops._send`, beside the lease check of floor S3) are refused when the remote is guarded
  and a commit they send holds a name, in its message, a path or an added line (`PushRefused`, which fails the run with
  the reason);
- the run's git hooks (`authorship.hook_script`, the `core.hooksPath` the run's git configuration names) run
  `python -I -m evo_agents.guard_check hook` for `commit-msg` and `pre-push`, then the checkout's own hook with what git
  gave it;
- a `gh` shim (`<worker home>/guard/bin/gh`) comes first on the agent's PATH: `gh pr|issue|release` commands that write
  text and `gh api repos/OWNER/REPO/...` calls with fields or `--input`, to a guarded repo, are refused when the text
  (flags, `--body-file`, standard input) holds a name; anything else runs the real `gh` unchanged.

A run in a container of an environment gets neither the hooks nor the shim: its git and PATH are the image's.

**Member** (`evo_agents/hub/guard_hook.py`). SessionStart keeps the policy in `~/.evo/hub/guard-policy.json` (a hub
without the plugin answers 404 and the file goes). The PreToolUse hook of Bash (`evo-agents hub hook pre-tool-use`)
denies a command that commits, pushes or writes with `gh` to a guarded repo when its text, a file it names for a
message or body, or the commits a push sends hold a name. The reason shows each name as the session wrote it.

**Hub** (`evo_agents/hub/guard.py`). A pull request the hub opens on a guarded repo (a chat or task run's draft, a
Curator change, a release) has each name in its title and body replaced with `[private]`; the log counts them.

## What it does not cover

An edit made in GitHub's web page, a commit pushed with `--no-verify` by a process outside a run, and text written to
a public repo outside GitHub's pull requests, issues and releases. The guard does not rewrite history: a name already
pushed stays in the commits and in the edit history of a pull request until someone removes it.
