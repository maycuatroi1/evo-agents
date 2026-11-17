# Building a knowledge graph

For a first project graph, follow [Getting started](getting-started.md#build-a-project-knowledge-base).
This reference covers source configuration, sync, storage and connectors.

`evo-agents harness init` starts a harness. Run in a repo's directory, or in a directory whose direct subdirectories
are repos, it writes a minimal `harness.yaml` and `knowledge.yaml` for those repos, with the origin (without the
password an https URL may carry) and the default branch git has for each. The project takes the directory's name,
unless `--project NAME` gives another. Each repo gets a git source and the harness a source of its own, all labelled
`internal`, with a Claude Code sink and a hub sink that clear `internal`. It overwrites nothing: when either file is
there already, it writes neither and says which is there. `evo-agents harness validate` accepts what it writes, and
`evo-agents hub project register` sends it to a hub ([docs/hub.md](hub.md#invite-links), Invite links, for a first
project of your own).

```sh
cd ~/code/acme-tools                 # api/ and web/ are git checkouts
evo-agents harness init              # or: evo-agents harness init ~/code/acme-tools --project acme
evo-agents harness validate
evo-agents hub project register      # as a hub admin, or with the right of a register invite link
```

## Sources and labels

A harness opts in with a `knowledge.yaml` next to its `harness.yaml`:

```yaml
version: 1
project: demo
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - id: claude-code@anthropic
      kind: agent-session
      clearance: {level: internal}
sources:
  - id: harness            # plans, contracts, bindings, docs of the harness itself
    connector: harness
    label: {level: internal, integrity: U}
  - id: app                # a repo declared in harness.yaml
    connector: git
    repo: app
    refresh: 1h            # due for kg sync --due once an hour
    label: {level: internal, integrity: U}
  - id: wiki               # any command that speaks kg/1
    connector: exec
    command: [my-cli, kg-connector, wiki]
    credentials: [{key: my.wiki.token, env: WIKI_TOKEN}]
    label: {level: customer, integrity: U}
identifiers:
  - kind: UseCase
    pattern: '\bKB-\d{2}\b'
```

A git source reads the text of markdown, YAML, text, config and code files (or of what its `include:`
globs match) and records other files by path only. It skips the paths its `exclude:` globs match, plus a
default list: build output, lock files, virtualenvs and agent folders such as `.claude/` and `.agents/`,
at any depth, and `state/` at the root only (a nested `src/state/` is ordinary code).
`allow:` takes globs that bring paths from that default list back, for example
`allow: [".claude/CLAUDE.md"]`, or `"*/.claude/CLAUDE.md"` for a nested one; `exclude:` still wins over
`allow:`. Only the git connector reads `allow:`.
`fetch: true` on a git source whose `ref:` is `origin/<branch>` fetches that branch from `origin` (with a
timeout) before each run, so the graph follows the remote without anyone fetching the clone. A failed
fetch keeps the run ok: it logs a warning and reads the ref as the clone has it. Without the key nothing
is fetched. Without `ref:` a source reads HEAD of its checkout, whatever branch that has, and
`harness validate` warns about it.
A git source also reads the history of its ref, one `Commit` node per commit with `changes` edges to the files it
changed, and, for a repo whose origin is on GitHub, the pull requests with their number, title, state, branches and
merge commit, never their body or comments (`pulls: false` turns that off; the token comes from `GH_TOKEN`,
`GITHUB_TOKEN` or `gh auth token`). A SHA or "PR #57" in a plan step's evidence then links the step to that commit
or pull request, and through it to the files: `docs/kg.md`, section Plans of the hub, commits and pull requests.
In a harness whose harness.yaml names a hub project, `kg sync` reads the plans at the revision the hub holds, with
the hub's label, rather than the copies in `plans/`; a machine not signed in to a hub reads the copies.
The graph says which commit each git source read and how many commits that is behind the remote's default
branch (`origin/<default_branch>`, counted from the clone without fetching): `kg status`, `kg_status`, the
SessionStart note and the hub's graph page show it, and `kg_node` links a file at that commit, so the lines
of a span match what the link opens.
Changing where a source reads from (a git source's `ref:`, `repo:` or `path:`, an exec source's `command:`)
makes its next run replace what the source read before, even when the old ref has newer commits; within
one ref, a file keeps the copy whose last commit is newest. Keys such as `refresh:`, `label:`, `include:`
or `fetch:` do not count as a change of place.
Before a file enters the corpus, the git connector replaces each secret in its text with a marker naming
its kind: a private key in PEM (`[redacted:pem]`), a provider's key (Anthropic, OpenAI, AWS, Slack, Google,
GitHub, hub tokens), the password of a URI, and the literal value of a variable or key named as a password,
secret, token, API key or private key (`DB_PASSWORD: [redacted:password]`). References such as `${VAR}`,
`os.environ[...]` or `process.env.X`, empty values, placeholders and descriptive words stay, and so does the
rest of the file, so its structure still enters the graph. The patterns favour precision over recall;
`evo_agents/kg/redact.py` lists what they take and leave.

A markdown file whose frontmatter `id:` matches an identifier pattern is where that code is defined;
a copy says `derived_from:` in its frontmatter, and `kg status` lists codes defined in more than one place.

Then, from the harness:

```sh
evo-agents harness validate          # schema and reference checks for every harness file
evo-agents kg sync --build           # run connectors, push their runs, build the graph on the hub
evo-agents kg status                 # freshness, coverage and held deletions per source
evo-agents kg build                  # ask the hub for a build of what it holds, and wait for it
evo-agents kg query kg_search "KB-01"
evo-agents kg query kg_impact --arg 'paths=["app:src/search.py"]'
evo-agents kg eval questions.jsonl   # Recall@k, MRR and tokens of kg_search, kg_context and grep (docs/kg-eval.md)
```

## Validation in CI

A harness's CI runs `harness validate` with the evo-agents of the tag `nightly`, the tip of main, which a workflow
moves every night:

```yaml
      - name: Install evo-agents
        run: python -m pip install "evo-ak @ git+https://github.com/maycuatroi1/evo-agents@nightly"
      - name: Validate manifest, contracts, knowledge and plans
        run: evo-agents harness validate .
```

A harness that must stay on one release pins it instead, `python -m pip install "evo-ak==<version>"`: the pin is
that harness's lock, and only an edit of the workflow moves it. The worker daemon follows the same tag unless its
owner locks its version ([docs/workers.md](workers.md#updating-itself-to-nightly)).

## Storage and redaction

Corpus and graph live in `~/.evo/kg/<project>/` (override with `EVO_KG_HOME`), never in a repo. Every
source sync and every build appends one line to `audit.jsonl` there: ids, times, counts and versions,
never item content. A sync's line also counts the secrets the git connector redacted, by item id and kind,
never their values. A build that finds nothing changed builds nothing and adds no line (see below).

Runs synced before the git connector redacted keep the old copies, in the source blobs the hub holds and the graph
files older builds uploaded. `evo-agents kg scrub` has the hub scrub the project of the current directory (`--project`
names one, `--all` takes every project; a hub admin, as `evo-agents hub kg scrub`): it lists each object holding a
secret by hash, kind and kind of secret, never the value, and changes nothing. `--purge` says what it is about to
delete, asks first (`--yes` when no terminal can answer), and has the hub delete the old ones. Sync and build first,
so the live copies become old ones (`docs/hub.md`, Secrets in old copies):

```sh
evo-agents kg sync --build                   # live copies become old ones
evo-agents kg scrub                          # count, change nothing
evo-agents kg scrub --all --purge            # delete the old copies of every project, after a yes
```

## Scheduled sync

To keep graphs fresh, give sources a `refresh` interval (`30m`, `6h`, `1d`). `kg sync --due` runs only
the sources whose interval has passed since the last ok run the hub holds of each; `--all` does that for every project
that has synced on this machine and takes a run from you on the hub. `kg sync` talks to the hub this machine is
signed in to and keeps no corpus here (`docs/hub.md`, kg sync against the hub). On macOS, `kg schedule install` loads
a LaunchAgent that runs `kg sync --due --all` every hour (`kg schedule print` shows the plist, `kg schedule
uninstall` removes it; output goes to `~/.evo/kg/schedule.log`). The schedule only syncs: graphs are built on the hub,
which queues a build with each run it ingests, from any member or the worker on duty. A project the schedule skips
(not on the hub, readable only, or with no hub sink) or whose push failed still gets builds from the runs of the
others. A plist of an older release, with `--build` or `--push`, still runs. To drop from
knowledge.yaml a source the hub holds runs of, take them off first with `evo-agents hub kg forget-source --project
demo --source old-repo`, then push; the other way round the push is refused (`docs/hub.md`, Dropping a source).

Syncs and builds do only what changed. A git source whose ref still resolves to the commit its last ok run read,
with the same config and connector code, is not read again: the sync records the run, writes no run log and leaves
the cursor, and `--due --build` builds only when a source read something. A harness source is always read, so
files not yet committed in the harness still reach the graph. The hub skips a build whose corpus state, config,
code and cursors are those of the latest build. A
build that runs links again only the items whose text or structure changed or that looked up a name or path that
now resolves elsewhere, and rewrites only the rows of the full-text index that changed. `kg serve` opens the store
read-only.

## Retention

The machine keeps no corpus, memo or graph, so nothing on it ages. The hub keeps the artifacts of each project's
newest graphs; `evo-agents kg prune` counts, on the hub, the artifacts of the older ones of the project of the current
directory (`--project`, or `--all`), and `--delete` deletes them (a hub admin, as `evo-agents hub kg prune`).

```sh
evo-agents kg prune --all                    # count, change nothing
evo-agents kg prune --all --delete           # delete the artifacts of the older graphs
```

## Trace a schema change across repos

The `schemas` repo owns `schemas/event.yaml`. The `service` repo consumes events described by that schema in
`consumer.py::consume`. With both repos indexed in your harness, add this to `bindings/schema.yaml`:

```yaml
version: 1
bindings:
  - from: service:consumer.py::consume
    rel: depends_on
    to: schemas:schemas/event.yaml
```

Build the graph, then ask what depends on the schema:

```sh
evo-agents kg sync --build
evo-agents kg query kg_impact schemas:schemas/event.yaml
```

The result includes the consumer in the other repo:

```text
hop 1:
  [service:consumer.py::consume] Symbol (parsed) line=1 -depends_on-> [schemas:schemas/event.yaml] (declared)
```

`declared` identifies the relationship you supplied. Without that relationship, this schema query does not flag
the consumer. Python module-name resolution stays within one indexed source: a module with the same name in another
repo is not evidence of a dependency. `kg_impact` lists code to check; it does not determine whether a schema change
is breaking. Read the [resolution rules](kg.md#cross-repo-bindings).

## Writing a connector

A connector is a generator in process or any executable. It writes JSON objects, one per line:
`hello` first, then `item`, `tombstone`, `listing`, `state`, `error`, `log`, and `closed` last.
Item IDs are `<source>:<kind>:<native key>`; every item carries a revision and a content hash over
canonical JSON. Deletions are only inferred from a scoped `listing` that ends with
`complete: true`, so a connector that dies half-way never deletes anything.

```sh
evo-agents kg connector test -- my-cli kg-connector wiki --fixture tests/fixtures/wiki
```

checks protocol, determinism, replay and permutation, truncated listings, golden output and labels.
