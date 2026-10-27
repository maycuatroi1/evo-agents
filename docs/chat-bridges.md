# Chat bridges

A group chat of the hub may be bridged to a group of a chat service, both ways: what members write in the group comes
into the hub's chat as theirs, and the hub's chat's messages go out to the group as far as each project lets them. The
hub knows no chat service: a core plugin that contributes a channel (`docs/plugins.md`, The core plugin API) does the
service's side through the API's `bridge_` functions (API 1.5). The sample plugin
`tests/fixtures/core_plugins/northwind_chat`, which `tests/hub/test_chat_bridges.py` drives through a fake service,
bridges a group with no Telegram anywhere.

## Linking

1. A chat admin, an admin of every project of the chat's agents, makes a one-time code on the hub from a web session:
   `POST /v1/chats/{id}/bridges/code` with `kind`, the kind of channel, or the plugin's own page through
   `bridge_code`. A machine token, a session that ended or anyone else gets 403; a direct chat or a chat without agents
   409; a kind no core plugin contributes 422. Each project of the chat's agents must enable the kind's plugin with
   `chats:read` and `chats:write`, or the code is refused (403). The code lives ten minutes; the hub keeps its SHA-256
   alone (`chat_bridge_codes`).
2. Someone in the group sends the code to the plugin (Northwind's `/bridge <code>`), which redeems it with
   `bridge_link(conn, plugin, kind, code, group_id)`. The checks of step 1 run again, as the code's maker. A code is used
   once; a group bridges one chat at a time (409 while it is bridged). The link is audited as `chat.bridge`.

`GET /v1/chats/{id}` lists the chat's `bridges`: id, kind, the group's id, who linked it and when.

## Unlinking

| From | How | Audited `via` |
| --- | --- | --- |
| the web | `DELETE /v1/chats/{id}/bridges/{bridge}` from a session of a chat admin | `web` |
| the CLI | `evo-agents hub chat unbridge CHAT BRIDGE` (the same route, a machine token) | `cli` |
| the group | the plugin's command (Northwind's `/unbridge`), `bridge_unlink(..., by="bot")` | `bot` |
| a hub admin | the same route when in the chat, or the plugin's admin route, `bridge_unlink(..., by="admin", actor=)` | `admin` |

An unlinked bridge carries nothing either way; its row stays with who unlinked it, when and from where.

## In: the group to the hub

The plugin finds the member who wrote the group's message (a member links their own account of the service as for
notifications) and calls `bridge_send(conn, member, bridge_id, body, external_id=, reply_to=, attachment_ids=)`. The
message goes through the hub's chat path as the member wrote it there: the same checks of who may write, mentions and
chat runs. It carries `via`, the bridge's kind. A group's message id is taken once, so a webhook delivered twice adds
nothing. `reply_to`, a group's message the bridge carried, puts it in that message's thread. A file goes first through
`bridge_attach`, which stores it as the member's attachment. A sender who linked no account gets nothing into the chat.

## Out: the hub to the group

Every new message of the chat, whoever wrote it, is the event `chat.bridge` (`what: message`) to the bridge's plugin,
except to the bridge it came in by: no echo. The event carries ids alone; the plugin reads the message with
`bridge_message`, which answers None unless every project the message's label names has a sink of kind channel for the
bridge's kind that clears it (`knowledge.yaml`, `docs/notifications.md`): no such sink, nothing goes out. The text goes
through the hub's redaction, and no file goes out, neither an attachment nor a run's output. `reply_to` is the group's
message its thread started from, so replies thread in the group too; `bridge_sent` records the group's id of what went
out. `bridged_to` on each message of the chat names the bridges that carried it.

A chat run that a message carried by the bridge started sends its state as `what: progress`, and an emoji put on or
taken off a message the bridge carried as `what: reaction`.

The event is stored for the bridge's plugin alone, in the first project of the chat's agents that enables it and grants
`chats:read`; with none, nothing goes out, and delivering it checks the grant again.

## Data

Migration 0165: `chat_bridges` (the link, the plugin's config and state, who linked and unlinked it),
`chat_bridge_codes`, `chat_bridge_messages` (a hub message and the group's message it came from, `in`, or went out as,
`out`), the column `chat_messages.via`, and the event kind `chat.bridge` of `plugin_deliveries`.
