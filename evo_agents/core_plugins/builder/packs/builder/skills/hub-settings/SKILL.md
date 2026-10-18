---
name: hub-settings
description: Propose a change of the hub's branding, a layer of features, a layer of a plugin's config or an agent profile of the project with the tool builder__propose, which an admin approves before the hub applies it.
---

# Proposing a change of a setting

The tool `builder__propose` takes one change:

```json
{
  "kind": "branding | features | plugin_config | agent",
  "summary": "what it changes and why, 1 to 500 characters",
  "target": {},
  "set": {"field": "new value"},
  "unset": ["a field the layer stops setting"]
}
```

The hub checks the change against the setting as it is now and answers `error: ...` naming each problem, with nothing
kept. A change that changes nothing is an error too. When it fits, the hub keeps it as a pending proposal, shows it in
the thread with its number and tells the admins who may approve it. It applies nothing.

## The kinds

| kind | target | set | who approves |
|---|---|---|---|
| `branding` | `{}` | `name` (at most 60 characters), `colors` | a hub admin |
| `features` | `{"layer": "hub"}` or `{"layer": "project"}` | feature id to `true` or `false` | a hub admin |
| `plugin_config` | `{"plugin": "<id>"}`, or with `"agent": "<handle>"` for that agent's layer | the plugin's config fields | an admin of the project |
| `agent` | `{"agent": "<handle>"}` | `name`, `description`, `instructions`, `packs` | an admin of the project |

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

## What happens next

An admin answers in the thread (`approve N`, `reject N`), or from the Inbox. Approved, the hub applies the change as
the setting's next version, only while the setting is still at the version the proposal was made on; otherwise the
proposal fails with the reason, and you propose again. `rollback N` brings back the version before an applied one.
Those messages are for the hub: say nothing about them.
