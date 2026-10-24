# Intake: from vague context to problems

An intake (I-1, I-2, ...) is one reading of vague context by the session of a member: a Messenger group, a chat.fpt.com
thread, a mail folder, a directory of documents, another harness, the hub's own chats. The session reads the sources
with the member's own access (the hub has no connector for them), cuts them into addressed pieces, keeps the pieces
that show a pain, and puts one result on the hub. Writers then approve what it proposed. The plan is
`problem-intake`; the web has no page of its own: proposals show in the Inbox, and problems with their evidence on the
Problems page.

## The life of an intake

1. `hub intake start --source KIND:LABEL[:NAME] ...` opens I-n, status `open`, with who asked and the sources. A source
   is its kind (`hub_chat`, `messenger`, `chat_fpt`, `email`, `file`, `harness`, `other`) and its label; the hub stores
   no raw content, only kind, label and optional name.
2. The session reads, and builds a document in format `evo-intake/1`.
3. `hub intake put I-n FILE` validates it, makes the proposals, the tasks and closes the intake (`done`). A second put
   is a 409.

```
evo-agents hub intake start --source messenger:internal:"Shop group" --source file:public:notes.md --project demo
evo-agents hub intake put I-1 result.json --project demo
evo-agents hub intake show I-1 --json --project demo
evo-agents hub intake list --status open --project demo
```

Starting and putting need the writer role; reading needs a grant on the project. Routes: `GET|POST
/v1/projects/{project}/intakes`, `GET|PUT .../intakes/{ref}`. MCP tools: `intake_list`, `intake_show`.

## evo-intake/1

The schema is `evo_agents/harness/schemas/intake.schema.json`, and the hub validates it on put. The document has:

- `candidates`: problems the session found. Each is either new or `matches` an existing `P-n`. A new one has the four
  fields of a problem (`statement`, `who`, `when`, `workaround`), each with a quote from a source or the label
  `inferred`. It carries `evidence` lines (the lines of `docs/problems.md`, Evidence) and may carry `confirmed_by`, a
  member of the project who confirmed it.
- `promises`: commitments seen in the sources (who, what, from which source, a date when it can be inferred, and
  whether evidence of delivery was seen).
- `questions`: what the session could not decide, each a `text` (with its hypothesis) and `for`, the person to ask.

## Sources and labels

Each evidence line and each source has a label. `public` and `internal` may go to the hub. `personal` never does: the
hub answers 422, and the session keeps such pieces on the member's machine. An excerpt is at most 300 characters.

## The rule of two independent sources

A source is independent of another when its kind or its address differs. A candidate with a single independent source
that no member confirmed is refused with 422 and a reason, and the put writes nothing. A candidate is confirmed by
`confirmed_by` or by a confirmed line of the problem it matches. For a match, the lines the problem already holds count
with the candidate's, so one new source can be enough.

## What a put makes

- A candidate that is new: a pending @builder proposal of kind `problem`, carrying its evidence. A writer approving it
  makes the problem and its lines together. Codes of one put follow each other, so approve them in order.
- A candidate with `matches: "P-n"`: a pending proposal of lines alone for P-n (`target.problem`), which writes no
  revision.
- Each promise: a task of the plugin tasks in the project, due on the inferred date, assigned to the promiser if a
  member of the project and otherwise to the member who put the result; the description says who promised, where, and
  whether evidence of delivery was found. Its external key is `intake:<project>:I-n:promise:<k>`. When the plugin is off
  the promises stay in the intake and the answer says `tasks_skipped`.
- Questions stay in the intake for the people named.

The session never sends a message, mail or comment to anyone: what it would say to someone else is a draft.

## Schema

Migration 0138 adds the table `intakes`; 0137 adds `problem_evidence`. The contract `hub-cli-v1` grew by additions only
(`hub intake ...`, `hub problem evidence ...`); `evo-agents hub contract print` prints it and evo-cli's tests read it.
