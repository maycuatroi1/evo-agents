---
name: hub-settings
description: Propose a change of the hub's branding, a layer of features, a layer of a plugin's config, an agent profile, the autonomy policy, a problem or a goal of the project with the tool builder__propose, which an admin (a writer, for a problem) approves before the hub applies it.
---

# Proposing a change of a setting

The tool `builder__propose` takes one change:

```json
{
  "kind": "branding | features | plugin_config | agent | policy | problem | goal",
  "summary": "what it changes and why, 1 to 500 characters",
  "target": {},
  "set": {"field": "new value"},
  "unset": ["a field the layer stops setting"]
}
```

The hub checks the change against the setting as it is now and answers `error: ...` naming each problem, with nothing
kept. A change that changes nothing is an error too. When it fits, the hub keeps it as a pending proposal, shows it in
the thread with its number and tells the members who may approve it. It applies nothing.

## The kinds

| kind | target | set | who approves |
|---|---|---|---|
| `branding` | `{}` | `name` (at most 60 characters), `colors` | a hub admin |
| `features` | `{"layer": "hub"}` or `{"layer": "project"}` | feature id to `true` or `false` | a hub admin |
| `plugin_config` | `{"plugin": "<id>"}`, or with `"agent": "<handle>"` for that agent's layer | the plugin's config fields | an admin of the project |
| `agent` | `{"agent": "<handle>"}` | `name`, `description`, `instructions`, `packs` | an admin of the project |
| `policy` | `{}` | a key of the autonomy policy by its dotted path | an admin of the project |
| `problem` | `{"problem": "P-<number>"}`, or `{}` for a new problem | `statement`, `who`, `when`, `workaround`, `metric`, `rank`, `owner`, `status`, `close_reason` | a writer of the project |
| `goal` | `{"goal": "G-<number>"}`, or `{}` for a new goal | `statement`, `metric`, `deadline`, `canvas`, `problem`, `rank`, `owner`, `status`, `close_reason` | an admin of the project |

- **branding.** `colors` is `{"light": {...}, "dark": {...}}`, each mapping the tokens `brand`, `brand-hover`,
  `brand-soft` and `on-brand` to `#rrggbb`. The hub refuses colours whose contrast falls under WCAG 2.2 AA. The logo and
  the favicon are uploads: a proposal may unset them, never set them.
- **features.** The ids are `chats`, `curator`, `kg` and `monitor`; `chats` and `monitor` exist on the hub's layer
  only. A feature is on unless a layer turns it off, the project's layer over the hub's.
- **plugin_config.** The plugin must be enabled in the project. The fields and their types are the plugin's config
  schema. A secret field is never set by a proposal, since you would see its value: tell the member to set it with
  `evo-agents hub plugin config`.
- **agent.** The profile must be one of this project's, and not its default agent. `packs` is the whole list, in
  order, each `{"plugin": "<id>", "name": "<pack>"}` with an optional `"version"`. An agent proposal unsets nothing.
- **policy.** The project's autonomy policy: what the hub may merge and release on its own. Name each key by its
  dotted path: `repos.<repo>.mode` (`off` or `auto`; `auto` only for a repo on GitLab), `repos.<repo>.target_branch`,
  `repos.<repo>.prod_release` (`immediate`, `after_uat` or `manual`), `repos.<repo>.after_uat_hours`,
  `repos.<repo>.jobs.uat_check`, `.prod_deploy`, `.prod_check` and `.rollback` (CI job names), `merge.opt_in_label`,
  `merge.risk_paths` (a list of globs), `merge.hold_until` (`merged`, `uat_check` or `prod_check`),
  `merge.pipeline_minutes`, `judge.worker`, `judge.model`, the `breaker.*` thresholds and `audit.sample_rate`. A key
  you unset goes back to its default. `evo-agents hub policy show --project <project>` prints every key with its
  value now. The repo must be one of the project's.
- **problem.** A problem of the project, before any solution: `statement` says what hurts, `who` who is hurt, `when`
  when it happens and `workaround` how they cope today (each at most 2000 characters, none empty). `metric` is
  `{"key": "<signal key>", "aggregate": "last | mean | sum | count | p50 | p90"}`, with `window_days` (1 to 90) and
  `tenant` optional, and no target: it shows how much the problem hurts. `rank` is its place among the problems (1
  first), `owner` a member with the writer role or above. `status` is `open`, `solved` or `dropped`; a solved or
  dropped problem names its `close_reason` in words. A new problem needs `statement`, `who`, `when` and `workaround`;
  the hub gives it the next code (`P-<number>`) and says it in the thread. You may unset `metric` and `close_reason`.
  `evo-agents hub problem list --project <project>` prints the problems now.
- **goal.** A goal of the project: what it is for, and how to tell it is reached. `statement` is one sentence (at most
  2000 characters). `metric` is `{"key": "<signal key>", "aggregate": "last | mean | sum | count | p50 | p90",
  "comparator": ">= | > | <= | <", "target": <number>}`, with `window_days` (1 to 90, 90 when left out) and `tenant`
  optional; the key must be a key of kind number of the project's registry of signals. `deadline` is a date
  (`2026-12-31`), `canvas` the slug of one of the project's canvases, `rank` its place among the goals (1 first),
  `owner` a member with the writer role or above. `status` is `active`, `paused` or `closed`; a closed goal names its
  `close_reason` (`achieved`, `dropped` or `expired`). A new goal needs a statement; the hub gives it the next code and
  says it in the thread. `problem` is the code of the problem of the project the goal answers, open or solved, at most
  one. You may unset `metric`, `deadline`, `canvas`, `problem` and `close_reason`. A goal has no budget.
  `evo-agents hub goal list --project <project>` prints the goals now.

## What happens next

A member who may approve it answers in the thread (`approve N`, `reject N`), or from the Inbox. Approved, the hub applies the change as
the setting's next version, only while the setting is still at the version the proposal was made on; otherwise the
proposal fails with the reason, and you propose again. `rollback N` brings back the version before an applied one.
Those messages are for the hub: say nothing about them.
