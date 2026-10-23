# Roadmap

evo-agents is early and before 1.0. Releases go to PyPI as `evo-ak`, each with its notes on the
[releases page](https://github.com/maycuatroi1/evo-agents/releases), and commands, routes and file formats can still
change from one release to the next. Nothing on this page has a date.

## Works today

- Plan runs. A worker on your own laptop or desktop takes a whole plan from your team's hub and runs its steps with
  Claude Code, Codex CLI or opencode. When the agent reports a step, the worker runs the step's verify commands again,
  then commits and pushes ([Plan runs](workers.md#plan-runs)).
- Decisions. A plan run's agent stops for a person only at decisions of a few kinds, such as deleting data or spending
  money, and makes every other choice itself ([Decisions](notifications.md#decisions)). You answer in the Inbox on the
  web or with a button on Telegram, and the wait does not count toward the run's timeout
  ([Decisions and notices](workers.md#decisions-and-notices)).
- A session for each step, when a plan run asks for one
  ([A plan run with a session for each step](workers.md#a-plan-run-with-a-session-for-each-step)).
- Long tool output. With the evo-hub plugin in Claude Code, a long result goes whole to a file on your machine and the
  model gets its start and its end ([hub.md](hub.md#memories-skills-and-knowledge-graphs)).
- One knowledge graph for a project and all its repos, which agents query over MCP
  ([Quick start](../README.md#quick-start), [kg.md](kg.md)).
- Invite links that give a person access to a hub ([Invite links](hub.md#invite-links)).

## In progress

These are in the code but not ready to rely on. Each stays off until someone turns it on, and how it works can still
change.

### The Loop

The Loop turns changes in a project, such as a revised goal or a plan an agent wrote, into work for the kinds of run
the hub already has, and asks people at three gates. It is off by default ([loop.md](loop.md)).

### Canvas

Canvas keeps a Lean Canvas for each product of a project, and a claim on it reaches validated only with real evidence.
It does nothing in a project until an admin of the project enables it ([canvas-plugin.md](canvas-plugin.md)).

### Growth plugins

Not built yet. One would follow the people who come to a project on their own. The other would post the project's
news through the project's own accounts, with a check before each post and a cap on spending.
