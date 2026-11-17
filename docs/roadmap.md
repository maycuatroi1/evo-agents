# Roadmap

evo-agents is early and before 1.0. Releases go to PyPI as `evo-ak`, each with its notes on the
[releases page](https://github.com/maycuatroi1/evo-agents/releases), and commands, routes and file formats can still
change from one release to the next. Nothing on this page has a date.

## Works today

- Centralized project knowledge base on the hub. Projects index Git repositories, uploaded `.zip`/`.tar.gz` archives,
  versioned Docs, SRS specifications, Lean Canvases, group chat threads and external connectors into one permission-gated
  graph ([Quick start](../README.md#quick-start), [kg-setup.md](kg-setup.md), [kg.md](kg.md)).
- Core and isolated plugins with decentralized markets. Core plugins and sandboxed `plugin/1` backends extend a
  project's Business cycle, Development cycle, Knowledge and Apps areas; hubs verify Sigstore or Ed25519 signatures
  from GitHub-hosted markets ([plugins.md](plugins.md), [market.md](market.md)).
- Visual multi-agent systems. Projects wire entry points, specialist agents, an Orchestrator, a nightly Curator and
  named knowledge graphs on dedicated worker and quota pools ([hub.md](hub.md)).
- Plan runs and verified checks. Workers on machines you control run plan steps with Claude Code, Codex CLI or
  opencode in isolated Git worktrees and re-run verification commands before recording completion
  ([Plan runs](workers.md#plan-runs)).
- Decisions and bounded autonomy. Runs pause only on actions that require human approval under the project's and
  member's autonomy policy, answered in the Inbox or on Telegram
  ([autonomy.md](autonomy.md), [Decisions](notifications.md#decisions)).
- Long tool output spooling and session memory sync in Claude Code and Codex through the `evo-hub` and `evo-kg` client
  plugins ([agents.md](agents.md)).
- One-time invite links for joining a project or registering a new one ([Invite links](hub.md#invite-links)).

## In progress

These are in the code and off by default per project; their behavior and configuration can still change.

### The Loop

The Loop turns changes in a project, such as a revised goal, a validated Lean Canvas claim or an approved SRS version,
into work for the project's specialist agents and plan runs, asking people at configured gates
([problems.md](problems.md), [loop.md](loop.md)).

### Canvas, SRS and Growth plugins

Lean Canvas ([canvas-plugin.md](canvas-plugin.md)), SRS and Tasks ([srs-plugin.md](srs-plugin.md),
[tasks-plugin.md](tasks-plugin.md)), plus core plugins for inbound CRM contacts and gated Marketing channels ship for
projects that enable them, while pilot workflows and third-party market tooling continue to mature.
