---
name: plugin-authoring
description: Write or change an evo-agents plugin in its repository - the manifest evo-plugin.json, the backend of plugin/1, pages and their iframe bridge evo-plugin/1, agent packs, tests, signing - and the CI workflow that publishes a version to the hub.
---

# Writing an evo-agents plugin

The run checks out the plugin's repository on its branch. You change it there, test it, commit and push; the hub opens
the draft pull request. You never publish, install or upgrade a plugin.

## The repository

```
evo-plugin.json        the manifest: id, version, publisher, tier isolated, engines, plugin_api 1, permissions,
                       config (a JSON Schema of its config), contributes
backend/               the backend of an isolated plugin: a process speaking plugin/1 on stdio
pages/                 the HTML of each page the manifest contributes
packs/<name>/          each agent pack: INSTRUCTIONS.md, skills/<skill>/SKILL.md, agents/, hooks.json, .mcp.json
tests/                 the plugin's own tests
.github/workflows/ci.yml   test, sign and publish (ci.yml beside this skill)
```

- **Manifest.** Raise `version` (semver) on every change that ships. `permissions` asks for what the plugin reads or
  writes on the hub; a project's admin grants them when they enable it, so ask for no more than the change needs.
  A new config field with a default keeps the projects that set none working.
- **plugin/1.** The backend answers JSON-RPC lines on stdin and stdout: `initialize` naming plugin/1 and listing the
  MCP tools the manifest contributes, `ping`, one method per point it serves (`tool`, `event`, `route`, `channel`,
  `job`) and `shutdown`. Nothing else goes to stdout; logs go to stderr. A tool's result is
  `{"content": [{"type": "text", "text": "..."}], "is_error": false}`.
- **Pages.** A page runs in `<iframe sandbox="allow-scripts allow-forms">` with no network of its own: it reaches the
  hub only through the bridge `evo-plugin/1` (`postMessage` to the parent, which calls the plugin's routes and tools
  for it). It injects nothing into the hub's web.
- **Agent packs.** A skill's directory name equals the `name` of its front matter, with a description of 1 to 1024
  characters. INSTRUCTIONS.md stays under 32 KiB.

## Before you push

```
python -m pytest -q                  # or the plugin's own test command
evo-agents plugin test .             # the checks a hub relies on
evo-agents plugin pack .             # <id>-<version>.tar.gz, deterministic
```

Commit with a message that says what changed and why. Push the run's branch. In your reply, say what changed, what you
ran and what it printed, and what the reviewer should check.

## Signing and publishing

The repository's CI does it, never you. `ci.yml` beside this skill is the template: copy it to
`.github/workflows/ci.yml` when the repository has none, with the plugin's directory in `PLUGIN_DIR`.

- **test** runs on every push and pull request: the plugin's tests, `evo-agents plugin test`, and the pack, passing on
  the bundle's file name and SHA-256.
- **sign** runs on the default branch only and is the only job with `id-token: write`. It runs none of the plugin's
  code: it packs again, checks the SHA-256 is the one the test job packed, and signs keyless with Sigstore.
- **publish** runs after sign, in the GitHub Environment `evo-hub`, whose reviewers and secrets the client's owner
  sets: `EVO_HUB_URL`, `EVO_HUB_LOGIN` and `EVO_HUB_TOKEN`. It publishes the signed bundle to the hub, where the
  version waits as available. The hub checks the signature against the publisher the operator trusts.

A hub admin then upgrades the plugin to that version (`evo-agents hub plugin upgrade <id>`, or the hub's plugin page),
and the hub installs it.
