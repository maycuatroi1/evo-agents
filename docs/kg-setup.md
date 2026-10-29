# Building a knowledge graph

For a first local graph, follow [Getting started](getting-started.md#try-the-knowledge-graph-locally).
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
evo-agents kg sync --build           # run connectors, build the graph
evo-agents kg status                 # freshness, coverage and held deletions per source
evo-agents kg build --verify         # rebuild from scratch and compare hashes
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

A corpus synced before its git connector redacted keeps the old copies: in the blobs of earlier runs, in memo rows,
and in free pages of the graph. `evo-agents kg scrub` (`--all` for every project directory of the kg home) reads
them with the connector's patterns and lists each blob that holds a secret by hash, path, item id and kind of
secret, never the value. A blob is old when nothing uses it, live when an item of the corpus refers to it now (a
sync of its source replaces it with a redacted copy, so sync and build first), and kept when only records the corpus
no longer holds name it, in the log of a run still in use (an item or a tombstone of that run still wins). Counting
changes nothing. `--purge` says what it is about to delete and asks first (`--yes` when no terminal can answer). It
then rewrites each run log in use that names a kept blob, keeping of its item and tombstone records only those the
corpus holds (the log keeps its name and format, and replays to the same state), deletes the old and kept blobs, the
run logs of old runs that name them and the memo rows holding a secret, compacts the memo and the graph so no free
page keeps the text, and appends a `scrub` line with the counts to `audit.jsonl`. It never deletes what the corpus
uses, and it holds every source's lock meanwhile, so a sync started then stops at once instead of writing.
`hub kg push` never sends a run the hub already holds, so a rewritten log leaves the machine only for a run the hub
lacks. In order:

```sh
evo-agents kg sync --build                   # live copies become old ones
evo-agents kg scrub                          # count, change nothing
evo-agents kg scrub --all --purge            # delete the old copies of every project, after a yes
```

## Scheduled sync

To keep graphs fresh, give sources a `refresh` interval (`30m`, `6h`, `1d`). `kg sync --due` runs only
the sources whose interval has passed since their last ok run; `--all` does that for every project that
has synced on this machine. On macOS, `kg schedule install` loads a LaunchAgent that runs
`kg sync --due --all --build` every hour (`kg schedule print` shows the plist, `kg schedule uninstall`
removes it; output goes to `~/.evo/kg/schedule.log`). On a machine signed in to a hub, the command also
gets `--push`, which sends the runs the hub lacks (`evo-agents hub kg push` does the same by hand). To drop from
knowledge.yaml a source the hub holds runs of, take them off first with `evo-agents hub kg forget-source --project
demo --source old-repo`, then push; the other way round the push is refused (`docs/hub.md`, Dropping a source).

Syncs and builds do only what changed. A git source whose ref still resolves to the commit its last ok run read,
with the same config and connector code, is not read again: the sync records the run, writes no run log and leaves
the cursor, and `--due --build` builds only when a source read something. A harness source is always read, so
files not yet committed in the harness still reach the graph. `kg build` skips a build whose corpus state, config,
code and cursors are those of the latest build (`--force` builds anyway; the hub's worker skips the same way). A
build that runs links again only the items whose text or structure changed or that looked up a name or path that
now resolves elsewhere, and rewrites only the rows of the full-text index that changed. `kg serve` opens the store
read-only.

## Retention

Old data on the machine goes by retention periods (`evo-agents kg prune`): `logs` (default 1d) for run logs, blobs
and rows of the runs table, `history` (1d) for the builds of the graph, and `memo` (14d) for memo rows after their
last use. An old run log is deleted once no record of it is the one the corpus holds, and one still in use is
rewritten without the records the corpus no longer holds, as `kg scrub --purge` does. A log the hub has not received
is never touched: `hub kg push` records which runs the hub holds, and a run of a source above the hub sink never goes
there. A blob goes once no log left and no item refers to it, the graph keeps its latest two builds and the builds of
the period, and a memo row goes when neither the period nor the latest build used it. None of it changes what a build
makes: `kg build --verify --cold` gives the same hash before and after. `kg prune` counts and changes nothing,
`--delete` deletes after a yes (`--yes` when no terminal can answer), and `--logs`, `--history` and `--memo` set the
periods of one pass. `kg prune --schedule on` counts, asks, and saves the periods in `~/.evo/kg/retention.json`: from
then on every `kg sync --due --all`, the LaunchAgent's command, prunes each project once a day and appends a `prune`
line with the counts to its `audit.jsonl`. The schedule is off until the owner turns it on.

```sh
evo-agents kg prune --all                    # count, change nothing
evo-agents kg prune --all --delete           # delete what the periods make old, after a yes
evo-agents kg prune --schedule on            # count, ask, then prune with every scheduled sync
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
evo-agents kg query kg_impact schemas:schemas/event.yaml --backend local
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
