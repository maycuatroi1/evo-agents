# evo-hub

The Claude Code plugin of the evo-agents hub. It carries a session to the team hub of its project: the memories,
plans and skills of the project over MCP (`evo-agents hub mcp`), and hooks that keep Claude Code's memory files and a
harness's plan copies in step with the hub. A session of a hub project also shows on the hub as a run of kind
`local`, with its log, its questions and the files it made. Everything the plugin sends is read and written with
this machine's sign-in (`evo-agents hub login --url URL`); not signed in, it sends nothing.

Install it from the marketplace of this repository:

```sh
claude plugin marketplace add https://github.com/maycuatroi1/evo-agents
claude plugin install evo-hub@evo-agents
```

The MCP server (`.mcp.json`) runs `uvx --from evo-ak==<version> evo-agents hub mcp ...` through `sh -c`, after a
`--version` call that downloads and caches that release. The hooks run `uvx --offline --from evo-ak==<version>
evo-agents hub hook ...` (`hooks/hooks.json`), the evo-ak release the plugin names, from uv's cache. When uvx cannot run
it (the plugin updated itself in an open session whose MCP server has not cached the new release, or the release never
reached PyPI), the server and the hooks run `evo-agents hub ...` from the PATH, the same package, and say so in one line
on stderr; with no `evo-agents` there either, their stderr line says they did not run. Every hook exits 0 within its
deadline, whatever the hub does. `docs/hub.md` (Local runs, Outputs of runs, Memories) has the whole of it.

## Hooks

| Hook | Command | What it does |
|---|---|---|
| SessionStart | `hub hook session-start` | pulls the project's memories, writes the plan copies of a harness, counts the skills to sync, and gives the session one line |
| UserPromptSubmit | `hub hook user-prompt` | registers the session as a local run at its first message and reports it running; hands the agent answers the member gave on the hub |
| PostToolUse | `hub hook post-tool-use` | writes a tool result longer than its threshold to `~/.evo/tool-output` and gives the model its head and tail |
| PostToolUse | `hub hook local-tool` | puts the agent's words and the tool that ran, redacted, in the run's log; queues the files the session sent and the images of the tool's result as outputs |
| PreToolUse (AskUserQuestion) | `hub hook local-ask` | puts the questions of the dialog to the hub as decisions of the run, with the files the session sent since the person's last message; it does not wait for an answer |
| Stop | `hub hook stop` | pushes changed memory files and the session's digest, reports the run idle |
| SessionEnd | `hub hook session-end` | reports the run ended |

`EVO_HUB_LOCAL_SYNC=0` turns the local run off (its log, questions and outputs); the agent of a worker run
(`EVO_RUN_ID`) never registers one, since its run is on the hub already.

## Outputs of a local session

The hub keeps the files an agent made as outputs of its run, for the people who read the run
(`GET /v1/projects/{p}/runs/{id}/outputs`, `evo-agents hub run outputs`). A local session sends:

- each file SendUserFile sends (the Code tab of Claude Desktop), with the tool's caption: source `sent`;
- each page the Artifact tool publishes, as its HTML (or Markdown) file, the caption holding the page's title and its
  claude.ai link: source `sent`;
- each file `evo-agents hub send FILE... [--caption TEXT]` names: source `sent`. A CLI session has no SendUserFile; the
  agent runs this command, inside the session (Claude Code names it in `CLAUDE_CODE_SESSION_ID`), after its first
  message. `--json` prints the outputs the hub recorded and the names still queued;
- every image in a tool's result, such as a browser's or Playwright's screenshot, or a Read of an image: source
  `tool`. The tool's line in the trace keeps a reference to the image (its SHA-256, type and size), never its base64.

A question the session asks with AskUserQuestion carries the files it sent (source `sent`) since the person's last
message: every view of the question lists them. The hook waits up to 3 seconds for those not recorded yet, then asks
without them.

The hooks never upload. They write an image's bytes and the list of what waits under
`~/.evo/hub/local-outputs/<session id>/`, then start `evo-agents hub send --queued` in the background, which uploads
them through the hub's presigned URLs and records them. A hub or blob store that does not answer leaves them there,
and a later hook (the end of the turn at the latest) starts the sender again. Queues of sessions untouched for 7 days
are deleted. `send.log` in that directory has one line per run of the sender: counts and names, never a file's
content.

What leaves the machine:

- a text file (Markdown, text, JSON, CSV, HTML, logs, code) goes through the same redaction as the run's events
  (`redact_data`, this machine's hub token among the values replaced), and so does a caption; the file on this machine
  stays as it was;
- an image, a video or a PDF goes as it is: nothing in it can be redacted. Do not screenshot a page that shows a
  secret.

On the hub:

- only the member whose session it is sees a local run and its outputs; a file attached to a question is seen by
  whoever sees the question;
- limits: 200 MiB a file, 500 outputs and 1 GiB a run, a file sent twice counted once; past one, the hub refuses the
  output and `send.log` names it;
- an admin of the project switches the images taken out of tools' results off (`PUT
  /v1/projects/{p}/output-settings` with `{"tool_images": false}`). Off, the hub refuses
  them and the machine sends none: the sender reads the switch before it uploads, deletes the images it holds, and the
  hooks stop taking them;
- images of source `tool` go with the run's events, `EVO_HUB_RUN_LOG_DAYS` (30 by default) after the run ended,
  unless a question carries them; files sent on purpose stay until the run's dispatcher or an admin deletes them;
- Telegram (`docs/notifications.md`, What never goes to Telegram): an image of a tool's result never goes there. Files
  sent on purpose reach the member who dispatched the run as one notice per run per minute at most, and the images of
  a question go with it, except for a restricted project, which gets the project's name, the kind of thing that waits
  and the link, no image and no file name.

`evo-agents hub run outputs RUN --project P` lists a run's outputs, and `--download DIR` saves them.
