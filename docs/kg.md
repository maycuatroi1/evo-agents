# The knowledge graph

How the graph of a project answers an agent: what `kg_impact` follows and covers per language, how the text of every
tool is written, who sees an element through the audience of its label, how `kg_search` finds nodes, what the note
after a grep adds, how plans of the hub, commits and pull requests enter the graph, which commit each git source read
and when a sync or a build does anything, how long this machine keeps old data, and how `kg eval` measures retrieval.
The README shows how to build a graph and call the tools.

## What kg_impact follows

`kg_impact` answers "what does this change reach": give it node ids, `repo:path` files or a unified diff, and it walks
back from those seeds to everything that depends on them, hop by hop. This part says which relations it follows, how
it reads a seed with many dependents, what each result carries, and which languages it covers.
`evo_agents/kg/extract/languages.py` holds the coverage tables this page prints, and `tests/kg/test_serve.py` fails
when the two disagree.

### Relations

Whoever has an edge into a reached node depends on it. `kg_impact` follows these relations, never through an edge of
status `proposed`:

| Relation | From, to | A dependent is |
| --- | --- | --- |
| `calls` | caller, callee | a function or method that calls the seed, in its own file or, through its imports, in another |
| `inherits` | subclass, base | a class that extends the seed, implements it as an interface, or takes it as a trait |
| `overrides` | method, method | a method of a subclass with the seed's name, which replaces it |
| `imports` | file, file | a file that imports the seed |
| `defines` | file, symbol | the file that defines a top-level symbol |
| `member_of` | member, class | a method or field of the seed |
| `depends_on`, `implements`, `verifies` | as declared | a step, a symbol that implements a use case, a test that verifies a symbol |

A call through `this`, `self` or `super` is a `calls` edge to the nearest class up the chain that defines the method
(the C3 order for Python), so a subclass that calls an inherited method depends on the base method. Documents and
sections that mention a reached node come back apart, under "documents to re-check".

A node is reached once, through its strongest edge, then the first relation by name. `rels` lists every relation it
has to that edge's target: a method that overrides its parent's and calls it through `super` comes back as `calls`
with `rels: ["calls", "overrides"]`, and the text line ends with `(also overrides)`.

A call into another file needs evidence: an import of the caller's file binds the name it calls, or the language puts
the callee's file in scope without one (the same Go directory, Java or Kotlin package, C# namespace). A call that only
shares its name with a symbol elsewhere gets no edge.

### Cross-repo bindings

Python module lookup is confined to the caller's indexed source, for imports, calls, base classes and return types.
Within that source, calls try the exact dotted module path, then suffixes for a `src/` layout. Imports use the exact
module path. There is no fallback to a same-named module in another source: each repo can install a different
package under that name. Even two sources of the same repo may read different revisions, so they remain separate
lookup scopes. This restriction does not change explicit bindings or references in documents.

Declare a cross-repo dependency with a qualified `from`, `rel: depends_on` and `to` in `bindings/*.yaml` (the
[README example](../README.md#trace-a-schema-change-across-repos)). The binding enters the graph as `declared`;
`kg_impact` follows it across sources. An endpoint that does not resolve fails the build instead of dropping the
dependency silently. Declaring a relationship asserts that it exists; the graph does not verify runtime behavior
or schema compatibility for you.

After upgrading the resolver, run `evo-agents kg build` for existing local graphs, or rebuild the project graph on
the hub. The link-stage version invalidates old memo entries, including on an unchanged corpus, so the new snapshot
removes previously inferred cross-source Python edges. Installing the code alone does not rewrite an existing graph.

### Seeds with many dependents

`kg_impact` reads the edges into a seed 500 at a time, up to 2,000: only edges of the relations above, of a status it
follows, that the session's sink may see. A seed with more says so instead of dropping the rest in silence: `cut`
holds `{"seed", "read", "total"}`, the text has a line such as
`cut: 6234 edges lead into [id] and the first 2000 were read; what depends on it through the others is not listed`,
and the summary ends with `; 1 seed(s) cut`. Edges are read in order of relation, so `calls` come first. For such a
seed, pass the symbols you change instead of their file, or a diff, and grep for the rest.

Beyond the seeds, a reached node with more than 150 edges is a hub: it comes back with `hub: true` and
`(hub, not expanded)`, and the walk does not go through it, since half the graph depends on it.

The text lists the first `limit` dependents of each hop (default 20, at most 500), strongest edge first and then by
id, and a quarter as many documents to re-check, those that mention the most reached nodes first, each with three of
the nodes it mentions. A hop or a group with more ends with a line such as
`... 6 more at hop 2: raise limit (up to 500), or pass the symbols you change instead of their file`. So
`kg_impact paths=["evo_agents/kg/store.py"]` on the graph of evo-agents (43 dependents, 55 documents) is about 6,100
characters, where it was over 25,000. The structured result keeps every dependent and document whatever `limit` is,
since the Curator's tier check reads them all, and says in `left_out` what the text did not list.

A text over about 25,000 characters ends with a handle for `kg_more`; the structured result then keeps only its
`summary`.

### The result

The structured result only gains fields: a client written for an older one reads `seeds` and `impacted` as before.
An MCP client gets the text alone, which is what its model reads; the structured result comes from
`evo-agents kg query kg_impact --json`, from the hub's route `POST /v1/kg/{project}/tools/kg_impact`, and to a
`tools/call` whose `params._meta` holds `"evo-agents/data": true`.

| Field | Holds |
| --- | --- |
| `build`, `depth` | the build read and the hops walked (1 to 3, default 2) |
| `seeds` | the nodes the ids, paths and diff named |
| `coverage` | one row per language of the code among the seeds: `calls_across_files`, `inheritance`, `overrides`, each `{"supported", "note", "missing"}` |
| `impacted` | per dependent: `hop`, `node`, `rel`, `rels`, `status`, `to` (the node it depends on), `hub` |
| `documents` | per document or section: `node` and the reached nodes it `mentions` |
| `cut` | the seeds with more edges than were read, with how many there are |
| `limit` | the dependents the text lists per hop |
| `left_out` | what the text did not list: `hops`, the number per hop (`{"2": 6}`), and `documents` |
| `ambiguous`, `unmatched`, `not_found` | paths that name several files or none, ids not found or not visible |
| `summary` | dependents and documents, and seeds cut |

The text says the same coverage in one line per language, for example
`coverage of swift seeds: calls across files no (the files of one Swift module ...); inheritance no (...); overrides no (...)`.
A `no` means callers of that kind are missing from the result: grep for them. A `yes` can still name what it leaves
out, `calls across files yes, not: ...` for Python: grep for those callers too.

### Coverage per language

The languages the git connector marks as code. Measured on graphify 0.9.73 with a two-file fixture per language in
`tests/kg/test_cross_file.py` and `tests/kg/test_inheritance.py`; a `no` is checked there to give no such edge.

| Language | Calls across files | Inheritance | Overrides |
| --- | --- | --- | --- |
| Python | yes | yes | yes |
| TypeScript | yes | yes | yes |
| JavaScript | yes | yes | yes |
| Vue | yes | yes | yes |
| Go | yes | no, Go has no inheritance | no, Go has no inheritance |
| Java | yes | yes | yes |
| Kotlin | yes | yes | yes |
| Rust | yes, names brought in by `use` | yes, `impl Trait for Type` | yes |
| Ruby | yes | yes | yes |
| PHP | yes | yes | yes |
| C# | yes | yes | yes |
| Swift | no, no import says where a callee lives | no, within one file only | no, within one file only |
| Dart | no, graphify reports no calls | no, no symbols | no, no symbols |
| Shell | yes | no, no classes | no, no classes |
| SQL | no, no symbols without tree-sitter-sql | no, no classes | no, no classes |

What each row takes as evidence:

- Python: `from m import f`, `import m [as x]` then `x.f()`, relative imports, imports inside a function, star
  imports; a parameter or local of the same name hides the import. Bases through the file's imports; `self.m()`,
  `cls.m()`, `super().m()` and `super(C, self).m()`. A method called through a receiver whose class the file states:
  an annotated parameter or variable (`Optional[C]`, `C | None`, `"C"` and `Self` too), `x = C(...)`, `x = f()` or
  `a, b = f()` where `f`'s return is annotated (`-> C`, `-> tuple[C, int]`, in any file), a `self` attribute that an
  annotation or an assignment in the class types, a property with a return annotation, `f().m()` and `C.m()`; the
  method is looked up the chain of that class, so an inherited one is found. A name gets a type only when every
  binding of it in its function agrees on the class (an assignment of `None` does not count). `C(...)` also calls the
  `__init__` nearest up `C`'s chain. Not covered, so grep for these callers: a receiver nothing in the file types (an
  unannotated parameter or return, a loop, `with` or `except` variable, an item of a container, `x.a.m()` through
  another class's attribute or an attribute a base class sets), `isinstance` narrowing, `getattr` and other dynamic
  calls, and an override reached through `self.m()` in its base class. Receiver types come from the `python-ast`
  backend, the default for Python; a source that sets `graphify-ast` gets none of them.
- TypeScript, JavaScript and Vue: named, default and namespace imports and `require()`, relative or through the
  `paths` and `baseUrl` of the nearest `tsconfig.json`; a receiver typed with an imported class. `extends` and
  `implements` are read off the class header, since graphify reports none for JS and TS; `this.m()` and `super.m()`.
  A Vue component counts the imports of its script block.
- Go: `pkg.F()` through an import whose path ends with the package's directory, and `F()` of another file of the same
  directory. Embedding a struct is not a subclass and is not followed.
- Java and Kotlin: single-type, wildcard, static and aliased imports, fully qualified names, the file's own package;
  `this.m()`, `super.m()` (`super<T>.m()` in Kotlin) and a bare `m()` of the class chain.
- Rust: a name brought in by `use crate::`, `self::` or `super::` and called bare; graphify reports no path call such as
  `a::f()` or `Type::new()`, so those link nothing. `impl Trait for Type` makes the type inherit the trait.
- Ruby: a method or `Constant.method` of a file loaded with `require_relative` or `require`; the superclass and
  included modules; `self.m()` and `super`. A bare `m()` links nothing.
- PHP: `use`, `use function`, the file's namespace, a file loaded with `require` or `include`; `extends`, `implements`
  and traits; `$this->m()`, `self::m()`, `static::m()`, `parent::m()`.
- C#: `using`, `using static`, aliases, the file's namespace and its parents; the base class and interfaces;
  `this.M()`, `base.M()` and a bare `M()` of the class chain.
- Swift: the files of one module see each other without an import, so nothing in a file says where a callee or a base
  class of another file lives. Within one file, `inherits`, `overrides` and calls through `self` and `super` are there.
- Dart: graphify's Dart extractor reports definitions and imports but no calls, marks no definition as callable and
  puts methods under the file, so a Dart file gets no symbols to link.
- Shell: a function of a file loaded with `source` or `.`, relative to the script or the repo root.
- SQL: graphify needs tree-sitter-sql, which the `graphify` extra does not install, so a `.sql` file gets no symbols.

`overrides` goes from a method to the method of the same name nearest up the chain of each base of its class, an
interface's or trait's declaration included, in every language whose inheritance row says yes.

A core install, without the `graphify` extra, gives symbols to Python files only: files in other languages enter the
graph as files, and `kg status` warns with the number of files per language and the command that installs the extra.

## The text of every tool

The text is what an agent's model reads, so it is kept short and readable:

- A file, a section and a symbol are named by a readable id: `evo-agents:evo_agents/kg/store.py`,
  `harness:docs/x.md#setup`, `evo-agents:evo_agents/kg/store.py::Store.search`, the form `bindings/*.yaml` uses. The
  stored ids (`evo-agents:file:evo_agents/kg/store.py`, `symbol:evo-agents:evo_agents/kg/store.py::Store.search`)
  stay in the structured result, and every tool takes both forms. Other ids (`plan:x/step:2`, `repo:x`) are as stored.
- A line names the node's kind, its name when the id does not end with it, its status, the line of a symbol, the
  status and number of a plan step, the chat and thread of a chat message, never its text. The URI is left to
  `kg_node`, whose props and evidence carry it. For a file of a
  git source on a forge, `kg_node` gives the link at the commit the source read (`.../blob/<sha>/path`), so a span's
  lines match the file it opens; the corpus keeps the link by branch, which does not change an item's hash on every
  commit (`evo_agents/kg/freshness.py`).
- `kg_status` names, per git source, the commit read and how far behind the remote's default branch it is:
  `read    app 1550c7a of HEAD, 3 behind origin/main`. On a machine with the checkout the count is taken again from
  the clone's `origin/<default_branch>` without fetching; the hub, which has no checkout, shows the count of the
  machine that synced the source.
- The label most nodes of a result share is printed once, under the first line (`label of every node: public,U`);
  a node with another label names it on its line, and a label with an audience ends with it
  (`internal,U audience chat:5 grant:demo:internal`). `kg_search` prints the status most of its results share once
  too, on its first line (`10 result(s) in project demo (build 7), status parsed`), and a line names a status only
  when it is another: `(resolved)`, `(public,U)` or `(resolved, public,U)`, nothing when both are shared. Its lines
  also leave out the name of a section its anchor spells and the number of a step its id holds.
- `kg_context` keeps its whole text, header, nodes and edges, within `budget_tokens`, counted as UTF-8 bytes / 3,
  and says how many nodes and edges it reached that did not fit (`left_out` in the structured result).
- Each tool takes, besides its own argument names, those agents often use for them (`evo_agents/kg/refs.py`,
  ALIASES): `files`, `targets`, `target`, `changed` or `name` for kg_impact's `paths`, `budget` or `max_tokens` for
  kg_context's `budget_tokens`, `source` and `target` for kg_path's `from` and `to`. Any other unknown name is an
  error that lists the tool's arguments. kg_impact reads a path that names no file as an id, and an id that names no
  node and looks like a path as a path.

## Audience

A label (`evo_agents/kg/policy.py`) has, besides a level, a location, an integrity and the projects it names, an
audience: a set of principals that a reader must all be to see the element. A principal is a string with a prefix, of
one of two forms:

- `chat:<id>`: a member of group chat `<id>` on the hub;
- `grant:<project>:<level>`: someone whose grant on `<project>` reaches `<level>` at least, on that project's own
  ladder.

A reader holds an audience too, and sees an element only when the element's audience is a subset of the reader's, as
for projects. Join takes the union, meet the intersection, and the order is the subset. An element with no audience
passes this dimension for every reader: its kg/1 hash and the answers of the tools stay what they were before labels
had an audience, so a connector that sets none has nothing to change. In kg/1 the audience is the optional key
`audience` of an item's label, a sorted list of principals without repeats. A list that is not sorted, repeats a
principal or holds a string of neither form cannot be read, and counts as the highest audience, which no reader holds:
the item is kept and nobody sees it (fail closed). The schema marks the key as a warning, so a sync keeps such an item
instead of refusing its run.

A node or an edge that several items derive shows to a reader who sees one of its derivations whole
(`policy.any_of`): it takes the least audience among the derivations at the lowest level and location, else the union
of theirs. A node that both chat 5 and chat 6 derive shows to a member of either chat, never to someone in neither.

The store keeps the audience of each unit, node and edge (`label_aud`) and reads as the session's audience
(`Store.reader`, none unless set), so a hidden element fills no limit, page or count, and `Session.visible` checks it
again. The shortcut that compares no labels when the build's highest label flows to the sink (`Store.top_label`) also
needs every audience of the build within the reader's (`Store.top_audience`). A `kg_more` handle keeps the audience its
result revealed and refuses once the reader no longer holds it. A result that reveals an element with an audience
names it in its label, in the structured result and in the READ_META of its text block, and the session label of
`~/.evo/kg/sessions/` joins it (`evo_agents/kg/sessions.py`), so a memory the session writes next carries it
(`docs/hub.md`, Memories, skills and knowledge graphs).

`evo-agents kg serve` on a machine holds no audience: an element with one never shows there, through any sink, and chat
history never enters a local graph. A local session finds chat messages through the hub instead, with
`evo-agents kg query kg_search TEXT --backend hub --arg 'kinds=["ChatMessage"]'` or the `kg_*` tools of
`evo-agents hub mcp`. On the hub a reader's audience is computed from its database at every read (`docs/hub.md`,
Projects and who sees what), and the core plugin chats labels each message with `chat:<id>` plus
`grant:<project>:<level>` for each other project its label names (`docs/plugins.md`, Chat history).

## Search

`kg_search`, the seeds of `kg_context` and the search box of the hub's graph pages all go through `Store.search`
(`evo_agents/kg/store.py`, `evo_agents/kg/search.py`):

- A node whose id, alias or name is the query comes first, each found through an index (`nodes_tx`, `nodes_name`,
  `node_alias`), never by reading the whole `nodes` table.
- Then the full-text index (`node_fts`, SQLite FTS5, `unicode61 remove_diacritics 2`), read three ways, each while the
  ones before found fewer nodes than asked: every word whole (AND), every word as a prefix (AND), then any word (OR).
  Words that only ask (`where`, `does`, `the`, `của`, ...) are dropped from a query that has others, so a question
  whose words no single node holds still gets ranked results. Up to 64 words of a query are read (`MAX_TERMS`): a
  question often names its subject late, and reading only its first 12 words cost a set of 300 news questions of
  some 30 words a fifth of their recall@10. The any-word way also leaves out a word the index cannot
  tell from one of them once it folds accents (`án` is `an` there, `cua` is `của`), which would match every one of
  them: `Phương án` and `phuong an` find the same messages.
- Each way ranks by BM25 over the node's name (weight 6), path (2) and body (1), times a light prior by kind: a plan,
  a step, a commit, a pull request, a directory or a chat counts half, a section or a requirement 0.7, a chat thread
  0.9 (every thread is named after its chat), a chat message as much as code, so at the same text score code and
  documents come before the work on them. The best 300 by BM25 alone are scored again with the prior.
- A node's score is that of the way that scores it best, times the way's weight: 2 for every word whole, 1.5 for every
  word as a prefix, 1 for any word (`STAGE_WEIGHTS`). The nodes of all ways are ranked together, so a node holding every
  word comes first at a like score but not whatever its score: a long page or a reply that quotes a message, holding
  every word once, no longer comes before the short section or message that matches most of them better.
- That score is then weighed by what the node is (`search.weight`). A word of the query that reads as a name (it holds
  `_`, `.`, `/` or `:`, mixes letters and digits, is camelCase or all capitals: `Store.search`, `HUB_DEGREE`) counts
  1.5 for the node it names (its name, its path or the end of its id), and such a node that no text way found ranks as
  the best of them. Background counts 0.6 unless the query asks for it: tests (`tests/`, `test_*.py`, `*.test.ts`),
  migrations, fixtures and golden files, generated JSON (`bundle-budget.json`, catalogs, baselines), closed plans
  (`plans/completed/`, a plan whose steps are all done, and its steps), reports and `docs/history.md`. Each kind is
  lifted by its own words only (`test`, `migration`, `fixture`, `golden`, `completed`, `đã đóng`, `report`,
  `history`, `lịch sử`, ...; `BACKGROUND_WORDS`): a closing step's title that says `đóng` asks for no history. On the
  tune part of the evo-agents questions this raised hit@5 of `kg_search` from 0.633 to 0.709.
- A project whose semantic mode is `on` also ranks by meaning: the vectors of a local embedding model, fused with the
  ranking by words by reciprocal rank fusion. Off by default, and a graph without vectors searches as above
  ([docs/kg-semantic.md](kg-semantic.md)).
- With `deprecated` false, the nodes a decision deprecated are left out before the list is cut, so they take no room
  in it.
- The rows of `node_fts_aud` (nodes with an audience, such as chat messages) are few, so the IDF FTS5 gives a word
  there is a fraction of the one `node_fts` gives it, and a message would rank below any symbol or section that
  matches no better. Each of them is scored as `node_fts` would score it instead: per phrase of the query, the BM25 of
  the phrase alone, over its IDF in `node_fts_aud`, times its IDF in `node_fts` (`Store._as_public`), so the rows of an
  audience the reader lacks weigh in no score through the IDF either.
- `kinds` is part of the query, so a search for sections gets as many sections as it asks for.
- `kg_search` follows each plan step, section or document in its results with up to three files, symbols or documents
  it mentions (`  mentions [id]` lines, `mentioned_by` in the structured result), through edges and nodes the sink
  may see: a search that finds a step or a page of the docs also names the code they point at.
- Each result `kg_search` found by its words that holds only some of the query's words says how many its name, path
  or body holds whole (` words=2/3` on its line; `matched` in the structured result counts them all), and a query of
  two words or more whose results hold none of them all says so under the header (`no result holds all N words of the
  query`) and gets at most 5 results (`WEAK_LIMIT`): the reader can tell a question the graph does not answer from one
  it does, instead of opening one file after another. A node listed
  because a hit mentions it gets no count. A document, named by its path, is also quoted: the passage of its body
  where the query's words gather, about 16 words, on the line below it (`    > ...`). A section is named by its
  heading and is not quoted. The count and the passage come from the same index rows the search read
  (`Store.matched`), so a node with an audience is counted and quoted only for a reader within it.

What the index holds for a node: its name, with the parts of a camelCase word added (`HubSession` also holds `hub` and
`session`; snake_case and paths come apart in the tokenizer); its path, a section's being its document's; and a body:
the text of a section or of an item that is neither code nor a plan (a chat message's among them, which no tool
returns), the description of a plan (goal, context), a
step (what, why, note) or a seam (notes), not their verify commands nor their evidence, the docstring of a Python
symbol or module, the comment right above a definition in another language or at the top of its file, and a
requirement's code. `đ` is indexed and searched as `d`. URIs and repo names are not indexed. A body longer than
100,000 characters is cut there, and the build counts the cut ones in its coverage (`search.cut`, with samples).

The index holds the latest build only. A build deletes and inserts only the rows whose content changed:
`node_fts_rows` keeps each node's row id and digest, and an index without it is written again whole. The build's
content hash covers the store's schema and what the index holds, so two builds with the same hash answer searches
alike. Store schema 3 added the index's
`kind` and `path` columns, `nodes_name` and `node_alias`; schema 4 added `label_aud`, the audience of a unit, node or
edge (`evo_agents/kg/policy.py`), and `node_fts_aud`, which holds the index rows of nodes with an audience apart, so
they never weigh in the ranking a reader without that audience gets. An older store is rebuilt from the corpus log by
the next `kg build` or `kg sync --build`, and readers refuse it until then.

## After grep

The evo-kg plugin runs `evo-agents kg hook post-search` after Grep, and after a Bash command whose pipeline starts with
`rg` or `grep` (`evo_agents/kg/post_search.py`, `evo_agents/kg/grep_command.py`). When a matched line falls in a node
of the graph, it adds a note beside the result, never in it:

```
evo-kg: what the project graph links to the code this search matched (callers, tests, plan steps); kg_context or kg_impact with an id gives more:
- evo-agents:evo_agents/kg/store.py::Store.search: callers evo-agents:evo_agents/kg/serve.py::Session.tool_kg_context, evo-agents:evo_agents/kg/serve.py::Session.tool_kg_search (+1 more); tests evo-agents:tests/kg/test_store.py, evo-agents:tests/kg/test_audit.py (+9 more); plan steps plan:kg-fresh-and-useful/step:5, plan:kg-fresh-and-useful/step:4 (+2 more)
```

- It reads the lines of the result: `path:line:text`, `path:text`, `path` (`-l`), `path:count` (`-c`), `line:text`
  of a search in one file, and the headings of `rg --heading`; the Grep tool's `content` and `filenames` too. A path is
  taken relative to where the command runs, after any `cd` before it, and must name a file the graph has.
- A matched line is placed in a symbol only when the graph agrees, since the checkout may differ from the commit the
  graph read: a line that defines a name (`def`, `class`, `function`, `func`, `const x =`, ...) goes to the symbol of
  that name in the file; any other line goes to the symbol around its line number only when that symbol calls a name
  the line holds. When a definition sits at another line than the graph has it, no other line of that file is placed
  by number. A line placed in no symbol stands for its file.
- For a symbol it names the callers (edges `calls`), the tests (test functions that call it and test files that
  import its file, those named after the file first) and the plan steps that name it or its file (`mentions`,
  `touches`, `implements`, `verifies`; steps not done first); for a file, the files that import it, the tests among
  them and the plan steps that name it. Two of each, then a count. A symbol whose definition matched comes first,
  then other symbols, then files, at most four lines after the header; a file whose symbol has a line gets no line of
  its own.
- Only edges of status `parsed` or `resolved` (never `proposed`) and nodes and edges the sink may see; only ids, no
  name or text of a source. Nothing to add, an unbound directory, no store, a result past 0.35 s of lookup, `-v`,
  `-L`, `--json`, `--files`, or a `grep` that filters another command's output: no note. Errors go to stderr as one
  line naming their type; the exit status is always 0.
- The note is `additionalContext`, so evo-hub's PostToolUse hook, which cuts a long result to its head and tail, cuts
  none of it, and both hooks read the same whole result. `EVO_KG_GREP_HINTS=0` turns the note off.
- It runs on every grep, so it starts lean: `evo-agents kg hook post-search` skips building the parsers of every other
  command (0.2 to 0.3 s), and reads the bound project from `hook-scope.json` under the kg home while the bindings,
  the project index, the harness registries, the harness.yaml above the directory and the project's harness.yaml and
  knowledge.yaml stay as they were (`evo_agents/kg/hook_scope.py`), so it imports neither the harness loader nor
  PyYAML. On 50 grep commands in the evo-agents checkout with the machine loaded (load average 21, 2026-10-18), the
  hook took 71 ms at the median and 109 ms at p95.

## Plans of the hub, commits and pull requests

A plan step names the commits and pull requests that did it; the graph follows those names to the files they
changed, so `kg_path from=plan:<plan>/step:<n> to=<repo>:<path>` answers "which step changed this file" in two hops:
the step `mentions` a commit, and the commit `changes` the file.

**Plans.** In a harness whose harness.yaml names a hub project, the hub holds the plans and `plans/` only copies of
them, which lag whenever nobody exported. `kg sync` then has the harness connector read the plans through the hub
(`evo_agents/hub/kg_plans.py`, `evo_agents/kg/connectors/hub_plans.py`): each plan the signed-in member sees becomes
the item `plans/<area>/<id>.yaml` holding its copy at the revision the hub holds, whatever the file on disk says,
with the hub's label on it (which raises the source's label, never lowers it) and a link to its page on the hub. One
request lists the revisions; a plan is fetched only when its revision is new to the source's cursor or its copy on
disk is not that revision. A plan the hub does not list (a draft not pushed yet) is read from disk. A machine not
signed in to a hub, or one whose hub does not answer (a warning on the run), reads the copies, except that a copy
which is the revision the cursor holds, as the hub wrote it, gives the very item the hub did, labels and all; so a
plan whose revision did not change gives the same item either way, and no build follows.

**Commits.** A git source reads, besides the files at its ref, the history of that ref: an item
`<source>:commit:<sha>` per commit, a node of kind `Commit` with `changes` edges to the files and documents it
added, edited or deleted that the graph still holds (a merge against its first parent, a rename as a delete and an
add, at most 100 paths, `files` counting them all). Its message is not read. The node keeps the author's email as
the commit records it, `author_email` in its props (no `.mailmap` applied), and its committer time, `time` in its
props, as UTC text that sorts, both labelled as its source like every other prop: the hub reads the authors of a
file's newest commits from them (`docs/srs-plugin.md`, Findings). The first run, and any run after the connector or the source's config changed (a new version of the connector
included, so commits read before a prop was added get it too), reads the whole history and lists it complete, so a
commit the ref no longer reaches goes, under the deletion guard; the runs after it read only the commits since the
commit of the source's cursor. When the ref no longer reaches that commit (a force-push, a history rewritten with
filter-repo, a reset to an older commit), the run reads the whole history again and warns `history of <ref>
rewritten`, so the commits only on the old history go. The guard treats those removals as any others: a rewrite of a few commits goes
at once, one of more than `max_removal_ratio` of them is held until `kg sync --accept-removals`, since a mistaken
force-push looks the same to the connector as a planned rewrite. A mention links to the commit read when exactly one has that SHA: `repo@sha` among the commits of
that repo, a bare SHA (7 to 40 hex digits, with a letter and a digit) in a step's evidence among the commits of the
step's `repo` first, then of every repo; a SHA no source read keeps a `Commit` node of its own, `commit:<sha>` or
`commit:<repo>@<sha>`, as before.

A commit's message is the item's text (redacted like a file, cut at 20,000 characters, labelled integrity U). The text
of a commit, of a pull request and the comments of a source file (a line comment or a `/* */` block of the common
languages) are read for the codes of the project's `identifiers`: a code gives a `mentions` edge from the commit, the
pull request or the file to the node `<kind>:<code>`, such as `usecase:UC CCACT-372`. An identifier may carry
`aliases`, `{alias: code}`: a mention of an alias (the old number `UC 41`) links to the node of its code, so no node
has the alias as its id. The hub's srs plugin gives these identifiers and the use case nodes, with `props.approval`
(`approved`, `in_review` or `draft`) and `props.aliases`.

**Pull requests.** For a repo whose `origin` in harness.yaml is on GitHub, the git source also reads its pull
requests (`evo_agents/kg/connectors/github.py`): an item `<source>:pr:<number>`, a node of kind `PullRequest`
named by its title, with `number`, `status` (open, closed or merged), `head` and `base` branches and
`merge_commit` in its props, and a `merged_as` edge to the commit that merged it when a source read that commit. The
comments of a pull request are never read; its body is the item's text, redacted like a file and cut at 20,000
characters, and the item is labelled integrity U for its title and body. A step
mentions a pull request by its URL, `owner/repo#57`, `<repo>#57` with a repo name of harness.yaml, or "PR #57" in
the text of a step whose `repo` is that repo. The first read takes the 3,000 most recently updated; later reads only
those updated since the newest the cursor saw (`pulls.since`), so a pull request is never removed. The token is the
source's credential `GH_TOKEN` or `GITHUB_TOKEN`, else that variable of the environment, else `gh auth token`; it
goes only into the request's header, never into an item, a log or an error, and a redirect is refused rather than
followed with it. Without a token no pull request is read (a line in the run's log). Since a git source whose ref
did not move is not read again, a pull request opened or closed meanwhile shows once the ref moves. `pulls: false`
on a source, or `EVO_KG_PULLS=0` on the machine, turns the reading off; the test suite sets the latter.

## Freshness

The graph is a snapshot of what its sources held when they synced. It says which snapshot, and it is built again only
when a source read something new.

**The commit read.** A git source ends each run with a cursor that names what it read (`evo_agents/kg/freshness.py`):
the commit, the ref, the remote's default branch it compares with (`base`: `origin/<default_branch>` of harness.yaml,
else the clone's `origin/HEAD`), how many commits of that branch the commit read lacks (`behind`), and, for an origin
on a forge, a link to the commit. `behind` is counted from the clone's remote-tracking branch without fetching; a
clone without that branch has no `base` and no `behind`. A build copies the read into the source's coverage, so the
graph says what it holds wherever it is read:

- `kg status` and `kg_status` name, per git source, the commit read and how far behind it is. The SessionStart note
  (and `kg status --brief`), which reaches every session, counts the sources and names only those not current:
  `commits read: 16 sources, 13 current; behind: app 1550c7a (3 behind origin/main)`, or `commits read: 16 sources,
  all current`; a source with no branch to count against is listed under `behind unknown`. On a machine with the
  checkout they count `behind` again with `git rev-list --count`, still without the network, and fall back to the
  count stored at sync.
- The hub has no checkout: it shows the count of the machine that synced the source, from the cursor of the newest run
  among those it merged. Its graph page has a card, Commits read, with a row per git source the member may see: the
  commit (a link to it on the forge, and a button that copies the SHA), the ref, and a badge when the commit is
  behind. For the projects with a night shift the hub also measures each git source against the default branch on
  GitHub every 6 hours: the commits behind and for how long, and the files the graph lacks, holds stale or still holds
  after their deletion (`docs/hub.md`, The graph's health; `evo-agents hub kg health`).
- `kg_node` gives a file's link at the commit read (`.../blob/<sha>/path`), so the lines of a span match what the
  link opens. The corpus keeps the link by branch, which does not change an item's hash on every commit.

Cursors come from connectors, and on the hub from any member's push, so a value that does not look like a commit, a
ref, a count or an https link is dropped before it reaches a tool's text or the SessionStart note. Nothing is fetched
beyond what `fetch: true` on a source already fetches.

**Syncs and builds do only what changed.** A git source whose ref still resolves to the commit its last ok run read,
with the same config and connector code, is not read again: the sync records an ok run, writes no run log and leaves
the cursor as it is, and `kg sync --due --build` builds only when a source read something. A harness source is always
read, so files not yet committed in the harness still reach the graph. `kg build` skips a build whose corpus state,
config, ontology, code and cursors are those of the latest build, names that build, and adds no line to the audit log
(`--force` builds anyway); the hub's build job skips the same way. A build that does run links again only the items
whose text or structure changed or that looked up a name or path that now resolves elsewhere, and rewrites only the
rows of the full-text index that changed (`node_fts_rows`, under Search). The first build after an upgrade that
changes the structure or link versions is cold.

## Retention on this machine

`evo-agents kg prune` deletes what this machine no longer needs from its kg home (`evo_agents/kg/retention.py`). Three
periods, each `0` or a number with `m`, `h`, `d` or `w`, say what is old:

| Period | Default | What it ages |
| --- | --- | --- |
| `logs` | `1d` | run logs, blobs and rows of the runs table |
| `history` | `1d` | the builds of `graph.sqlite` and the rows only they read |
| `memo` | `14d` | memo rows, from their last use |

What a pass deletes in a project directory:

- A finished run log of a run that started before the logs period, when no record of it is the one the corpus holds;
  one that still holds some is rewritten without the others, as `kg scrub --purge` does, so the logs left replay to
  the same state. A log the hub has not received is never touched: `hub kg push` records which runs the hub holds,
  and a run whose source's label is above the project's hub sink, or of a project that declares no hub sink and never
  pushed, goes to no hub. A project whose knowledge config does not load keeps every log no push recorded.
- Blobs older than the logs period that no log left and no item refers to.
- Rows of the runs table older than the logs period whose log is gone or never was, except the newest run and the
  newest ok run of each source (`kg sync --due` and `kg status` read them) and runs whose records the corpus holds.
- Builds that finished before the history period, except the latest two, so a reader that opened the previous build
  just before a new one landed still reads it whole.
- Memo rows that no key used within the memo period nor in the latest build; a corpus that stays the same keeps the
  rows of its latest build. `--all` also prunes the memo the projects share.

None of it changes what a build makes: `kg build --verify --cold` gives the same hash before and after. A SQLite file
is compacted only when at least a tenth of its pages are free.

```sh
evo-agents kg prune                          # the project of the current directory: count, change nothing
evo-agents kg prune --all --delete           # every project: say what goes, ask, delete (--yes when no terminal)
evo-agents kg prune --logs 3d --memo 30d     # other periods for this pass
evo-agents kg prune --schedule on            # count, ask, then prune with every kg sync --due --all
evo-agents kg prune --schedule off
```

The schedule is off until the owner turns it on. `--schedule on` saves the periods in `<kg home>/retention.json`;
from then on `kg sync --due --all`, the command the LaunchAgent of `kg schedule install` runs, prunes each project at
most once a day, after its sync and push, holding every source's lock so no sync writes meanwhile. Each pass that
deletes appends a `prune` line with the counts to the project's `audit.jsonl`. `--json` prints the counts for a
script.

## Clusters

A build groups the graph into named clusters in two levels when the optional extra `clusters` is installed
(`pip install 'evo-ak[clusters]'`, which pulls `graspologic-native`; the hub image has it). Without the extra the build
still succeeds with the same graph and no clusters, and `evo-agents kg status` says why on the line `clusters: none,
graspologic-native is not installed: ...`. A build made before clusters existed opens as before and reports `clusters:
none, the build predates them`. The code is `evo_agents/kg/clusters.py`.

**The projection.** Clusters are computed over a projected graph, not over every node:

- a Symbol counts as the File that defines it (its `props.file`, else the File of its `defines` edge), and a Section
  as the Document that holds it;
- Source, Repo, Directory and Commit nodes are left out, and so are the structural relations `in_source`, `in_repo`,
  `child_of`, `defines`, `part_of` and `member_of`;
- image files, and plan steps whose name recurs across plans ("Merge and release"), are left out as noise;
- only nodes and edges every reader of the project may read take part: no audience, and a level and location within
  the clearance of the project's hub sink (else the lowest clearance its sinks declare, else level 0). A node of a
  narrower audience is neither a member nor a word of a label.

The weight of a pair of projected nodes is the number of edges that join them, divided by the square root of the
product of their degrees. The most connected 0.5% of the projected nodes stay out of the clustering and join
afterwards the cluster their edges reach most, so a few documents every plan names do not glue unrelated clusters
together. A shared node with no projected edge belongs to no cluster.

**The algorithm.** `hierarchical_leiden` of graspologic-native, seed 7, resolution 1.0, gives level 1, the clusters of
the whole graph, and level 2, the parts of a level-1 cluster over 150 members. A level-2 cluster exists only under a
level-1 cluster that large. An error of the engine does not fail the build: the coverage records `computed: false` with
the reason.

**Labels.** A cluster's label is its first 5 words by c-TF-IDF over the names and paths of its members, against the
other clusters of its level, with file suffixes and common English and Vietnamese words left out. Each cluster also
keeps its size, its members counted by kind, and the weight of its links to the other clusters of its level.

**Stability.** The seed is fixed, and the clustering starts from the clusters of the previous build (the store's last
ready build, or the target `graph.sqlite` when the hub builds into a staging store). A new cluster takes the id of the
previous cluster of its level it matches with a Jaccard index of at least 0.5, best matches first and each id once;
any other cluster's id is a digest of its members. The same graph, previous clusters and parameters give the same
clusters and the same content hash. The algorithm's version, its parameters and the engine's version are an input of
the build, so installing or upgrading the extra rebuilds. Each build keeps the members it started from in
`cluster_warm`, outside the content hash, so `kg build --verify` reproduces a build whose previous build is gone (the
hub writes each build into an empty store). The tables `clusters`, `cluster_members` and `cluster_links`
count in the content hash only when they hold rows, so a build without clusters hashes as it did before.

**On the hub.** The hub serves the clusters per reader: it recounts sizes, kinds, link weights and labels over the
members and edges the reader can see and never serves the stored numbers, so a hidden node cannot leak through a
count or a label word. It keeps the last views it counted, by the artifact read and the reader's clearance and
audience, so the explorer's reads of the map, a cluster and the whole graph count a view once
(`evo_agents.hub.kg_map.ViewCache`). The project's knowledge graph explorer draws the map; `docs/hub.md` (Memories,
skills and knowledge graphs) lists the routes and `evo-agents hub kg clusters`.

## Review

Each build with clusters computes a review queue: what in the graph a person should look at, found by six rules with
no model (`evo_agents/kg/review.py`, stored in the build's `review_items` table).

| Rule | What it finds | Suggested action |
| --- | --- | --- |
| `dangling_mention` | an item (a document, a plan) that mentions code the graph does not hold, the dangling mentions its source's coverage counts | `fix_mention` |
| `unmentioned_document` | a Document no other node mentions or links to, its own sections aside | `link_or_deprecate` |
| `cluster_without_knowledge` | a level-1 cluster with code (File, Symbol) and no Document, Plan, Problem or Claim | `add_knowledge` |
| `possible_duplicate` | nodes of one kind whose normalised names (accents dropped, case folded, letters and digits only) are the same and whose ids differ | `merge` |
| `ambiguous_match` | an item whose mentions matched several nodes by name, or a path edge resolved to one of several files with that path | `disambiguate` |
| `bulk_noise` | images, generated files (lockfiles, `dist/`, `node_modules/`, `*.min.js` and the like) and plan steps whose name repeats across plans, 3 or more of a kind in one place | `exclude` |

An item holds its rule, the ids of the nodes it is about (its evidence, at most 50 with the full count in its detail),
the level-1 cluster its first node counts in, the suggested action and what it found. Kinds whose names repeat by
design (Symbol, Section, File, PlanStep and the like) are not checked for duplicates.

**Fingerprints.** An item's fingerprint, `r:` and 16 hex digits, hashes its rule with what it is about: a node id, an
edge id, a cluster id, a kind and name, a directory. Nothing that changes between builds goes in, so the same finding
keeps its fingerprint across builds.

**Dismissing.** A writer of the project dismisses an item with a reason, on the web's review page or with `evo-agents
hub kg review dismiss`. The hub keeps the dismissal by fingerprint (`kg_review_dismissals`, migration 0141), so the item stays
dismissed in later builds. Dismissing an item again replaces the reason and keeps the first build. There is no undo.

**Who sees what.** A reader sees an item only through its nodes: the first nodes the rule requires must all be
visible to them, the rest of its evidence is shown only where visible, and an item left with too few visible nodes
(two for a duplicate) is not shown. The hub recomputes each item's cluster label and order for that reader, so the queue names no hidden node, id or count.
The page groups items by rule, then by cluster, most connected nodes first.

**Health.** The open items of each rule are a level metric of the graph's health, `kg_review_<rule>`, measured as the
owner through the hub's sink, with dismissed fingerprints left out. The Curator's `kg_health` lens reads them
(`docs/hub.md`, The graph's health).

## Decisions

A writer of a project corrects its graph on the hub with decisions, and the next build applies them. A decision never
deletes anything: it is recorded with its reason and can be undone. Fixing what a source says (a wrong document) still
goes through a commit to that source.

**Kinds.**

- `deprecate` a node, or an edge given by from, relation and to, with a required reason. The node or edge stays in
  the graph with `props.deprecated = {reason, decision}`.
- `merge` two node ids, as `same_as` of a binding file does: the smallest id stays and the other becomes its alias.
- `link` two nodes with a relation the project's ontology knows. The edge has status `declared` and the prop
  `decision`.

**States.** A decision is `proposed`, `active`, `rejected` or `undone`. A writer's decision is active at once. An
agent only proposes, through the MCP tool `kg_propose` of evo-hub (it needs the writer role, returns no graph data, and
judge runs cannot call it); a writer accepts or rejects the proposal with a reason. A writer undoes an active decision.
`orphan` is not stored: the hub computes it when it lists decisions, for an active decision whose nodes the latest
build no longer holds (a link or a merge counts only its nodes). An orphan applies to nothing and breaks no build; it
shows in the list for a writer to undo.

**Revisions.** A project has one decision revision, the highest of its events (0 before the first). Every write names
the revision it read (`base_rev`), and the hub answers 409 with the revision now when it differs; nothing changes.
Each step records who took it, when, why and the build the graph was at (`kg_decisions`, `kg_decision_events`,
migration 0142). A new decision may name only nodes and edges the writer sees in the latest build. Taking, accepting
or undoing a decision queues a build.

**How a build applies them.** The hub gives the active decisions to the build as one item of its own source
`plugin.hub_decisions`, a binding item marked `curation`, with the project's default label
(`evo_agents/hub/kg_decisions.py`). The binding stage reads it (`evo_agents/kg/pipeline/curation.py`): merges join
like `same_as`, links become `declared` edges, and deprecations mark their node or edge once the graph is assembled. A
decision whose ids the graph does not hold is skipped, never an error. A project with no active decision gets no item.

**Effect on the tools.** `kg_search`, `kg_context`, `kg_path` and the web's node search leave deprecated nodes and
edges out; `kg_impact` does not follow a deprecated edge; `kg_node` of a deprecated node says `deprecated: <reason>
(decision N)`. Each of `kg_search`, `kg_context`, `kg_node`, `kg_impact` and `kg_path` takes `deprecated: true` to
include them. A merged id answers as an alias of the node that stayed.

**Who sees what.** A member sees a decision only when every node and edge it names that the build holds is visible to
them. Writers decide, readers of the project see the list, and anyone without a grant gets 404. `evo-agents hub kg
decisions` lists and takes them (`docs/hub.md`, Memories, skills and knowledge graphs).

## Measuring retrieval

`evo-agents kg eval questions.jsonl` asks `kg_search` and `kg_context` each question of a set as an agent would,
greps the same checkouts for comparison, and prints Recall@1 and @5, MRR, the share of empty answers, answers per
1,000 tokens of output and the time per call, by question type and method. `kg eval-questions` turns the done steps
of the hub's plans into questions whose answers are the files their commits changed. The command ships without
questions: a team keeps its set in its harness and runs it before and after a change to search or to the text of the
tools. [docs/kg-eval.md](kg-eval.md) gives the format of the set, the methods and the columns.
