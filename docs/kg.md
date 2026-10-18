# What kg_impact follows

`kg_impact` answers "what does this change reach": give it node ids, `repo:path` files or a unified diff, and it walks
back from those seeds to everything that depends on them, hop by hop. This page says which relations it follows, how
it reads a seed with many dependents, what each result carries, and which languages it covers. The README shows how to
build a graph and call the tools; `evo_agents/kg/extract/languages.py` holds the coverage tables this page prints, and
`tests/kg/test_serve.py` fails when the two disagree.

## Relations

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

## Seeds with many dependents

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

## The result

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

## Coverage per language

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
  status and number of a plan step. The URI is left to `kg_node`, whose props and evidence carry it.
- The label most nodes of a result share is printed once, under the first line (`label of every node: public,U`);
  a node with another label names it on its line.
- `kg_context` keeps its whole text, header, nodes and edges, within `budget_tokens`, counted as UTF-8 bytes / 3,
  and says how many nodes and edges it reached that did not fit (`left_out` in the structured result).
- Each tool takes, besides its own argument names, those agents often use for them (`evo_agents/kg/refs.py`,
  ALIASES): `files`, `targets`, `target`, `changed` or `name` for kg_impact's `paths`, `budget` or `max_tokens` for
  kg_context's `budget_tokens`, `source` and `target` for kg_path's `from` and `to`. Any other unknown name is an
  error that lists the tool's arguments. kg_impact reads a path that names no file as an id, and an id that names no
  node and looks like a path as a path.
