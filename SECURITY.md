# Security policy

evo-agents runs coding agents that read your repositories, hold credentials for them and push changes. A flaw in it
can reach your code, your accounts or your hub's data, so please report one privately.

## Reporting a vulnerability

Use GitHub's private reporting: the **Security** tab of this repository, then **Report a vulnerability**
([direct link](https://github.com/maycuatroi1/evo-agents/security/advisories/new)). Do not open a public issue, pull
request or discussion for it, and do not post it in a hub's group chat.

Include:

- the part affected: the CLI, the knowledge graph, the worker daemon, the hub API, the web, a plugin in `plugins/` or
  a file in `deploy/`;
- the version: `evo-agents --version`, or `version` from the hub's `GET /v1/health`;
- the steps that reproduce it, and what an attacker gains.

Leave out real credentials and private project data. If you found a credential that is exposed, name where it is
and its kind, not its value.

The maintainers answer in the advisory, work on the fix there with you, and credit you in it unless you ask
otherwise. The fix ships as a patch release of `evo-ak` on PyPI and of the hub images on GHCR, and the advisory
names the first release that has it.

## Supported versions

evo-agents is before 1.0. Fixes go into the latest release only, so upgrade to it before you report, and after a
fix ships: `uv tool upgrade evo-ak` for the CLI and worker, the image tag of that release for a hub
([docs/hub-deployment.md](docs/hub-deployment.md)).

## Scope

In scope is everything in this repository, and in particular:

- a run's agent or a plugin getting past the guard, the sandbox or the autonomy settings
  ([docs/guard.md](docs/guard.md), [docs/autonomy.md](docs/autonomy.md));
- a member, worker or run reading or changing a project, credential or memory its grant does not cover
  ([docs/hub.md](docs/hub.md#projects-and-who-sees-what), [docs/credentials.md](docs/credentials.md));
- a secret reaching a knowledge graph, a log, a transcript or a commit that the redaction should have kept out
  ([docs/kg.md](docs/kg.md)).

Out of scope: flaws in the coding agents themselves (Claude Code, Codex CLI, opencode), which go to their vendors, and
a hub run against the advice of [docs/hub-deployment.md](docs/hub-deployment.md), such as one whose secrets are
copied from another hub.
