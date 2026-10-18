# Plugins: the plugin/1 protocol and the plugin host

This is the reference for what a plugin's code does on a hub: the extension points it contributes to, the backend of
an isolated plugin, the protocol that backend speaks, where it runs, the permissions a plugin asks for, and how its
author checks and signs it. The store, the manifest, signatures, enabling in a project and config are in
`docs/hub.md`, section Plugins; a hub for a client, with its own publishers and plugins, in `docs/hub-deployment.md`.

A plugin is core or isolated (`tier` in the manifest). A **core** plugin is a Python package in the hub's image; its
contributions run in the api and the worker processes. An **isolated** plugin, the default, carries a **backend**: a
program the hub runs outside its own processes, in a plugin host, and reaches over the `plugin/1` protocol. An isolated
plugin never runs Python in the hub; it serves its routes, MCP tools, jobs, events and channels from its backend, and
its pages run in a sandboxed iframe that reaches the hub only through the bridge `evo-plugin/1` (section Pages).

## The backend and its manifest

An isolated plugin that serves any extension point declares a `backend` in `evo-plugin.json`:

```json
{
  "id": "sandbox",
  "version": "1.2.0",
  "tier": "isolated",
  "backend": { "runtime": "python", "entry": "backend/main.py", "requirements": "backend/requirements.txt" }
}
```

- `runtime` is `python`, the only runtime of plugin API 1; the host runs the entry with a Python of its own release.
- `entry` is the program inside the bundle, run as `__main__`.
- `requirements` is an optional requirements file inside the bundle, and the wheels it names sit in a `wheels/`
  directory next to it. The host installs them into the plugin's own virtual environment from those wheels and from
  nothing else (no index, no URL, no directory, no source distribution), so installing runs none of the plugin's
  code.

The paths stay inside the bundle. A core plugin has no backend; an isolated plugin that only contributes pages and
config may omit it.

The host reads the requirements file before pip does and refuses it at the first line that is not one requirement of
this form: `name==version` followed by at least one `--hash=sha256:<64 hex>`. A backslash at the end of a line
continues it and `#` starts a comment, so the output of `uv pip compile --generate-hashes` or `pip-compile
--generate-hashes` passes as it is. Options (`-r`, `-c`, `-e`, `--index-url`, `--find-links`), paths, URLs, extras and
markers are refused, since each of them makes pip read another file, reach the network or run a build backend. The
version is a PEP 440 version in its normalized form (`1.2.3`, `1.0rc1`, `2.0.post1`, `2.1.0+cpu`), and never one that
ends like an archive (`.tar.gz`, `.zip`, `.whl` and the others pip knows): pip reads `foo==1.tar.gz` or `foo==1+x.zip`
as a file of its current directory and builds it. pip then reads a copy the host wrote, with `--no-index --find-links
<wheels> --only-binary=:all: --require-hashes`, under the backend's own user, in an empty directory of root's:

```text
backend/requirements.txt    httpx==0.28.1 --hash=sha256:...   (and every dependency, pinned and hashed)
backend/wheels/             httpx-0.28.1-py3-none-any.whl, ...
```

`pip download --only-binary=:all: -d backend/wheels -r backend/requirements.txt`, run for the platform of the hub's
image (Linux, the image's Python), fills the directory.

## The plugin/1 protocol

The host starts the backend and speaks JSON-RPC 2.0 to it, one JSON object per line in UTF-8: the host's requests on
the backend's stdin, the backend's responses on its stdout. **stderr is for logs only**; anything else on stdout
breaks the protocol. Every request has an id and gets one response with that id; the backend may answer in any order.
plugin/1 has no request from the backend to the host: a backend that needs the hub calls the hub's HTTP API with the
token `initialize` gave it. The definitions are in `evo_agents.plugins.protocol` (`PARAMS`, `RESULTS`).

Methods:

- **`initialize`** (first, once): the protocol, the plugin's id and version, the hub's URL and the plugin's token, the
  effective config of the hub's own layer (`instance`) and of each project that enabled it (`projects`) with the
  secret fields decrypted, and what each project granted (`grants`). The result names the protocol and the plugin
  back, and lists the MCP tools the backend serves, each with an input schema.
- **`ping`**: an empty object, while the backend serves.
- **`route`**, **`tool`**, **`job`**, **`event`**, **`channel`**: one call to a contribution to that extension point,
  naming the contribution and the project (section Extension points says when the hub makes each). Their params are
  open objects, so a later minor version of plugin/1 adds keys without a new major: a `job` names the projects that
  enable the plugin (`projects`), an `event` its kind (`kind`), and an `event` or a `channel` the hub's id of the
  delivery (`delivery`), the same on every try.
- **`shutdown`**: an empty object, then the backend exits; the host kills it if it does not.

Errors are JSON-RPC's: `-32700` for a line that is not JSON, `-32600` for a message that is not a request, `-32601`
for an unknown method, `-32602` for bad params, `-32603` for the backend's own failure; `-32000` to `-32099` are the
backend's to define.

A minimal backend, standard library only, is `tests/fixtures/plugins/sample/backend/main.py`.

## Where a backend runs, and the secret boundary

The backends run in the **plugin host**, `evo-agents hub plugin-host`, a service apart from the api and the worker
that holds **none** of the hub's variables: not `EVO_HUB_DSN`, not `EVO_HUB_SECRETS_KEY`, not the blob store keys. It
is given the hub's URL, the shared secret `EVO_HUB_PLUGIN_HOST_TOKEN` and a data directory, and nothing else. In the
compose file it is the `plugins` service, built from the same image, running as root with only the capabilities
SETUID, SETGID, CHOWN and KILL, `no-new-privileges`, an init as pid 1, its own volume at `/plugins`, and limits on its
CPUs, memory and processes (`EVO_HUB_PLUGINS_CPUS`, `EVO_HUB_PLUGINS_MEMORY`, `EVO_HUB_PLUGINS_PIDS`). The default
of 2048 processes lets fifteen backends sit at their own limit of 128 and still leaves 128 to the host, whose kill of
a plugin's processes starts no process at all.

**Each backend runs under a user of its own**, `UID_BASE` (200000) plus the plugin's id on the hub, with a group of
the same number and no other group. The host gives one only as root, and a host that is not root **refuses to start
a backend** (its state says why), unless it is started with `--allow-shared-user` on a developer's machine. The users
below 200000, from 100000 on, are the killers' (below), so a plugin whose id is 100000 or more gets none: the hub
refuses to install its backend and the host starts none. Under a
shared user the backends' environments and arguments stay apart, but their files, the host's `/proc` (its environment
holds the shared secret) and each other's `/proc` in the moment after exec do not; `evo-agents plugin test` runs the
author's own plugin that way. `evo_agents.plugins.environment.isolation` says which mode is in force (`uid` or
`process`), and so does each backend's state.

For each plugin at a version, under the host's data directory:

- `code/<name>/<version>/` holds the bundle's files, written by the host, read-only; the directory is root's with the
  plugin's group, mode 0750, so another backend cannot even list it;
- `envs/<name>/<version>/` is its own virtual environment, built with `python -m venv --without-pip`, then filled by
  pip running under the backend's user from the bundle's wheels; afterwards the host makes root own every file pip
  wrote (following no link), removes every link and set-id file of that user, which pip never writes, resets the
  modes (0755 for a directory, no write but root's for a file), and the directory is root's with the plugin's group,
  mode 0750. The marker of a directory written whole (`.evo-ready`) is created, never opened through a link;
- `data/<name>/` is the one directory the backend writes, its `HOME` and, under `tmp`, its `TMPDIR`, owned by its user
  with mode 0700. The host has neither CAP_FOWNER nor CAP_DAC_OVERRIDE, so at each start it takes the directory back
  (chown to root), sets the modes as owner, replaces a `tmp` that is not a directory, and hands both back.

The host builds each backend's environment **from nothing** (`evo_agents.plugins.environment.backend_env`): `PATH`,
`HOME`, `TMPDIR`, `LANG`, `VIRTUAL_ENV` and the plugin's name, version and protocol, and no more. Nothing of the
host's environment reaches a backend, so even a host started by mistake with a hub variable does not pass it on. A
backend's secrets arrive only in `initialize`, on its stdin, and are its own plugin's: the hub decrypts the secret
config fields where the key is and never puts another plugin's secret in a backend's payload.

A backend starts through a **trampoline**, the host's own Python run isolated (`-I -S`, so no `.pth` file and nothing
of the current directory runs). Still as root, it first closes every file descriptor but 0, 1 and 2: uvloop (the loop
uvicorn picks when uvicorn[standard] is installed) ignores `close_fds`, so a descriptor the host holds, or a copy of
the backend's own pipes, would otherwise reach the plugin. Then it takes the backend's user and group itself, with no
other group, and exits 71 unless it holds exactly them as its real, effective and saved IDs with no capability left;
the host passes no user to the event loop's `subprocess_exec`, which uvloop would refuse. A backend that keeps the
host's user (isolation `process`) never runs with root as its real, effective or saved user, nor with a capability:
the trampoline refuses, except for `evo-agents plugin test`, which runs the author's own plugin and says so. Then it changes into the data directory and sets the resource limits
before any code of the plugin runs, and execs the environment's Python (`-I`). The limits are no core file, 1024 open
files, 4 GiB of address space per process and 128 processes and threads for the plugin's user. Then a **launcher**
marks the backend **not dumpable** (`prctl(PR_SET_DUMPABLE, 0)`) and runs the entry; the host marks itself the same
way at start.

Stopping a backend kills its process group and then every process of its user, again until `/proc` shows none, so a
child that left the group with `setsid()` goes too. The host does it **itself, as root** (CAP_KILL): pass after pass
over `/proc`, it sends SIGSTOP to every process whose real or saved user is the plugin's, until a pass finds none of
them running, then SIGKILL to each. It signals each process through a descriptor of its `/proc` directory, taken
before it reads the status there (a pidfd for `pidfd_send_signal`), so a pid freed and taken again never gets the
signal. A stopped process forks nothing and signals nothing, and no process of the plugin may signal, renice, move to
another CPU or trace the host, whose user is root; SIGSTOP and SIGKILL can be neither caught nor blocked. So neither
signals nor tricks with scheduling delay the kill, and it needs no new process, so it works with the service at its
pids limit. What a pass cannot catch is a chain of processes each forking the next and exiting at once; for that a
**killer** then calls `kill(-1)`, which reaches every process its user may signal at once (a fork under way fails
rather than leave a child). The killer's effective user is the plugin's, so it may signal every process of the plugin,
but its real and saved user is one of its own (100000 plus the plugin's id), so no process of the plugin may signal
it; whatever it meets, it is killed after 10 seconds and its round fails. One kill gives up after 30 seconds in all,
however many processes escape its rounds, and the caller tries again on its own schedule. A child a backend makes
with `clone(CLONE_PARENT)` is the host's child, which nothing else waits for: once it has ended, the host reaps it
when it stops the backend and before the next start, so it holds no pid, nor one of the processes the user's limit
allows. Placing runs in a thread that cancelling a start does not end, so stopping also kills pip and waits for that
thread, and the backend that replaces one, under the same user, starts only then. A plugin no longer installed is
stopped in the background, so its stop holds neither the loop that keeps the other plugins running nor any of them;
while processes of its user outlive that stop, or the stop fails with an error, the host kills them again with a
backoff until `/proc` shows none, and its state in the host's status, which reaches `GET /v1/plugins/{name}/backend`,
says so. A start that cannot kill every process of the plugin's user places nothing and starts nothing: it backs
off. Why a backend is down, a failed start's text included, is for a hub admin, in the
backend's status; a project member calling the plugin reads only that its backend is not running now.

The doors a secret could leave by, and what shuts each:

| Door | Shut by |
| --- | --- |
| the backend's environment and arguments | built from nothing; secrets only on stdin |
| a file descriptor of the host's | the trampoline closes all but 0, 1 and 2, as root, before the plugin's code runs |
| `/proc` of the host, docker-init, the api or another backend | another user's process (and not dumpable); the api and worker are other containers |
| another plugin's code, environment or data | root's files of mode 0750 with that plugin's group; data 0700 of another user |
| a requirements line that runs code or reads a file | only `name==version --hash=...` lines, a copy the host writes, pip under the backend's user |
| the host's HTTP port, which backends share a network with | the shared secret; the api may call only `ping` and the points' methods, never `initialize` or `shutdown` |
| the api's routes for the host | outside `/v1` (`/internal/plugin-host/`), which the reverse proxy does not forward, and the shared secret |

A hostile plugin that tries every door and reports what it reads, writes and may signal is
`tests/fixtures/plugins/hostile`. `tests/plugins/test_plugin_isolation.py` proves it reads nothing, and
`tests/plugins/test_plugin_isolation_root.py`, which CI's `plugin-host-root` job runs as root in the hub's image with
the service's capabilities and a fresh volume at `/plugins`, proves it also writes into no other plugin's directories
and signals no process of another user, that pip runs under the backend's user, and that stopping a backend leaves no
process of its user behind, even with a process of that user stopping every other one, or a hundred spinning on
one CPU while others demote whatever kills them.

### The hub token of a backend

The hub mints a backend's token when the host is about to start it, and only then: the host pulls the plugins and
their payloads from `GET /internal/plugin-host/plugins`, which mints nothing, and asks
`POST /internal/plugin-host/plugins/{name}/token` just before each start, which revokes the plugin's older tokens.
A pull that finds nothing changed restarts nothing. Removing a plugin, or the hub turning it off, revokes its tokens at
once, and the host stops its backend at its next pull (every 60 seconds). Minting locks the plugin's row as both of
those do, so neither can slip between minting's read and its insert, and a token opens nothing once its plugin is not
installed or is turned off, whatever its own row says. A plugin's token is not a user token and never signs in: only the
plugin API under `/v1/plugin-api/` takes it (a backend hands tasks to its agent types there, section Agent types), and
each request may do only what the plugin's installed version asks for and the project granted. When the hub calls a
plugin, the extension points check on that call that the plugin is installed, turned on, enabled by the project and
granted what the call needs (section Extension points).

## Crashes, hangs and restarts

The api calls the host with a timeout of its own, so a backend that hangs never holds an api request open past it: the
host (`evo_agents.plugins.host`) fails the call with `BackendUnavailable` and the api answers a 503. A backend starts
in a task of its own as soon as the host places it, so placing a bundle and installing its wheels never holds a call
either: a call waits for a start at most its own timeout. A backend that crashes or hangs is stopped and started
again after a backoff (1 s, 2 s, 5 s, 15 s, 30 s, then 60 s), every process of its user killed so nothing it spawned
lingers; the host checks every 5 seconds for a backend whose backoff has passed. The host reports each backend's state to the hub, which a hub admin reads:

```sh
curl -H "authorization: bearer $TOKEN" https://hub.example.org/v1/plugins/sandbox/backend
# {"plugin": "sandbox", "state": "running" | "starting" | "placing" | "backoff", "isolation": "uid",
#  "pid": 1234, "restarts": 0, "error": null, "started_at": "...", "retry_at": null, "reported_at": "..."}
```

## Permissions

A manifest lists the permissions the plugin asks for (`permissions`), from a fixed set
(`evo_agents.plugins.manifest.PERMISSIONS`). An admin of a project reads each one in words before enabling the
plugin, and enabling grants exactly that list (`docs/hub.md`, section Plugins in a project). What a plugin may do in a
project is what the project granted that its installed version still asks for, checked on every call:

| Permission | The admin reads | Opens in this release |
| --- | --- | --- |
| `runs:read` | read the project's runs, their events and their outcomes | the `run.state` events; the bridge's `runs.list` and `runs.get` |
| `plans:read` | read the project's plans | the `plan.step` events; the bridge's `plans.list` and `plans.get` |
| `chats:read` | read the project's group chats | the `chat.message` events |
| `memories:read` | read the project's memories | the bridge's `memories.list` |
| `notifications:send` | send notifications to the project's members | an isolated plugin's channel, which gets the project's notifications |
| `runs:dispatch` | dispatch runs in the project | handing tasks to the plugin's own agent types the project enabled, through the hub's API for plugins, and the `task.result` events (section Agent types) |
| `chats:write` | post messages in the project's group chats | nothing yet: no route of the hub posts a plugin's message |

A plugin's routes, MCP tools, jobs and pages need no permission: they work in every project that enabled the plugin,
for the callers the hub lets through (sections Extension points and Pages).

## Extension points

A plugin contributes to the hub's extension points in its manifest (`contributes`). A **core** plugin names the Python
of each contribution (`python`, `module:attribute`), which runs in the api or the job worker; an **isolated** plugin's
contribution runs in its backend, which the hub calls through the plugin host with the matching plugin/1 method. Both
tiers take the same params and give the same results: the hub calls a core plugin's function as `handler(params,
context)` with plugin/1's params and a `CoreContext` (the plugin, the hub's engine for its own tables, the config, the
project), and checks every result against `plugin/1`'s `RESULTS` whichever tier answered, so a result the hub cannot
use reaches no caller (`evo_agents.hub.plugin_calls`).

**Every extension point has a version of its own** (`evo_agents.plugins.compat.POINT_VERSIONS`), the keys a
contribution takes and the calls the hub makes for it. A contribution names the version it is written against with
`api` (1 when it names none); a release that does not serve it refuses the plugin at publishing and installing as
incompatible, and turns it off if a hub upgrade drops it. This release serves version 1 of every point:

| Point | Keys of version 1 | Core | Isolated |
| --- | --- | --- | --- |
| `channels` | `name` | a `notifications.Channel` class for the members' channels of kind `name` | every notification of a project that enabled it and granted `notifications:send` |
| `routes` | `name`, `methods` (isolated), `prefix` (core) | a FastAPI `APIRouter`, included under `/v1/x/<id>` | `/v1/x/<id>/<project>/<name>` and the paths below it |
| `mcp_tools` | `name`, `description`, `input_schema` | an async function of the `tool` params | method `tool` |
| `jobs` | `name`, `cron` | an async function of the `job` params | method `job` |
| `events` | `name`, `on` | an async function of the `event` params | method `event` |
| `tables` | `name` | a table of the hub's schema the plugin owns | (core only) |
| `pages` | `name`, `title`, `icon`, `menu`, `entry` (isolated) | a native page of the hub's web, at `/p/<project>/x/<id>/<name>` | an HTML file of the bundle under `pages/`, framed in a sandbox (section Pages) |
| `runtimes` | `name`, `title`, `acp`, `models`, `secrets`, or `python` (core) | an ACP agent (`acp`), or an adapter class of a runtime of the core, which replaces its built-in adapter on the worker (`python`) | an ACP agent (section Runtimes) |
| `environments` | `name`, `title`, `image`, `network`, `mounts` | a container image a run's agent runs in, chosen by the environment axis of its agent profile | the same (section Environments) |
| `agent_packs` | `name`, `title`, `description` | a directory `packs/<name>/` of the bundle laid out as a Claude Code plugin, which an agent profile attaches at a version of the plugin; a plugin of the hub's image has no bundle to build it from | the same (section Agent packs) |
| `agent_types` | `name`, `title`, `description`, `profile`, `packs`, `environment`, `jobs`, `events`, `pages` | a template of an agent profile with the plugin's own packs, environment, jobs, events and pages it uses, which a project enables and the plugin hands tasks to | the same (section Agent types) |

What a project does not enable is not there: every route, tool, job, event and channel of a plugin works in a project
that enabled the plugin while the plugin is installed and turned on, and the hub checks it on each call, not once.

- **Routes.** Every path under `/v1/x/<id>/` starts with a project, `/v1/x/<id>/<project>/...`, and answers a member
  who holds a grant there, or 404, as for a project the caller cannot see. The hub authenticates the caller first, as
  for any path under `/v1`. A core plugin's router is included at `/v1/x/<id>` with that check as a dependency of each
  route; its handlers use the hub's own dependencies for the caller. An isolated plugin's route takes the methods it
  declares (`GET` when it declares none, else 405), and the hub hands its backend the method, the path below the
  route, the query, the body in base64 (1 MiB at most), the request headers `accept`, `accept-language`,
  `content-type`, `if-modified-since`, `if-none-match` and `user-agent` (never the caller's credential or cookies), and
  the caller: its login, the project and its role there. The answer is served from the hub's origin with only its
  status, its body and the headers `cache-control`, `content-language`, `etag` and `last-modified`: a content type
  other than JSON, plain text, CSV, a raster image or bytes becomes `application/octet-stream` with
  `content-disposition: attachment`, and every answer carries `x-content-type-options: nosniff` and
  `content-security-policy: default-src 'none'; frame-ancestors 'none'; sandbox`, so a backend sets no cookie and runs
  no script on the hub's origin. A backend that is down or past 30 seconds answers 503, its error or a result plugin/1
  does not allow 502. The proxy is not in the OpenAPI document: what a plugin's route takes is its own.
- **MCP tools.** A plugin's tool is `<id>__<name>` on the hub's `/mcp`, after the hub's own 23. It is listed, and
  answers, in a session whose project enabled the plugin, for a caller who holds a grant there. For the agent of a run,
  the run's agent profile decides too: a tool axis of mode `all` lets plugins' tools through, an allowlist only those
  of the plugins it names as `mcp:<id>`, and a judge run's agent gets none. The description and the input schema are
  the installed version's manifest's, which its publisher signed: a backend cannot change what an agent reads about a
  tool, only a new version that a hub admin upgrades to can. Arguments are checked against the schema before the
  plugin sees them.
- **Jobs.** A job runs when its `cron` (five fields, UTC; `0 * * * *` when it names none) takes the minute, while a
  project enables its plugin, through the hub's job queue: `hub.plugin_jobs` queues one `hub.plugin_job` for it, with
  the queueing lock `plugin-job:<id>:<name>`, so a job still waiting is not queued twice. The call names the projects
  that enable the plugin and may take 120 seconds.
- **Events.** A plugin enabled in a project takes the events its `on` names there (every kind when it names none) that
  its grant lets it read: `run.state` with `runs:read` (a run moved: its id, kind, states, actor, plan and step),
  `chat.message` with `chats:read` (a message in a chat with an agent of the project: its ids and who wrote it),
  `plan.step` with `plans:read` (a step's status changed: the plan, the step, the statuses and the revision), and
  `task.result` with `runs:dispatch`, only to the plugin whose agent type ran the task (section Agent types). A payload
  carries ids and states, never a text the project's labels may keep from the plugin; the plugin asks the hub for more.
  The hub writes one delivery per plugin in the transaction that made the event (table `plugin_deliveries`), and the
  job `hub.deliver_plugin_events` hands each to its plugin every minute, **at least once**: a delivery counts only once
  the plugin answered, a failed try is tried again after 1, 2, 4 minutes and so on up to an hour, and the eighth fails
  it; before each try the hub checks again that the project enables the plugin and grants the kind. The `delivery` id
  stays the same on every try, so a plugin that must act once keeps the ids it has seen. Deliveries that ended are kept
  14 days.
- **Channels.** A core plugin's channel is a kind of members' channel, sent with the notifications' own outbox and
  rules (`docs/notifications.md`), as Telegram's is. An isolated plugin's channel gets each notification of a project
  that enabled it and granted `notifications:send`, through the same outbox as events, with the member, the kind, the
  link and the words a restricted project's Telegram message carries (`Run failed`), never the title or the body; it
  answers `delivered: false` to refuse one, which fails that delivery at once.

### Core plugins

A core plugin is a Python package installed beside evo-agents that names its manifest in the entry point group
`evo_agents.plugins`. Installing it is what trusts it, and its code runs in the hub's processes. The hub reads the
group once per process; the routers are included when the app is made. Each core plugin of the image is recorded in
the hub's store as the api starts, as a version of signature kind `image`, published and installed by nobody, which the
store neither installs, upgrades nor removes (409): the image does. One that projects enable (it asks for a permission,
takes a config, or contributes an MCP tool, a job, an event, a page or a route under `/v1/x/<id>`) they then enable,
grant what it asks for and configure like any other, and its install is audited. One that does none of that runs
hub-wide: the store lists it, installed, with no audit row (it ran before the store listed it), and no project's list
does; reading, enabling or configuring it in a project is a 404.

**A core plugin owns tables through the hub's migrations.** It lists them under `tables`, each named with its prefix:
its id with `_` for `-`, then `_` (`telegram_links` for telegram, `srs_docs_items` for srs-docs). Their revisions are
in the hub's one chain of migrations, applied under the hub's lock with the rest; the plugin never creates a table at
run time. The hub records a core plugin only when its schema has every table the plugin owns. Turning the plugin off,
for a project or for the hub, deletes nothing: its tables and rows stay for when it is on again.

evo-ak ships its core plugins in `evo_agents.core_plugins`, declared in its own entry points, so every install has
them, the hub's image among them. No package of evo_agents imports them (import-linter contract
core-plugins-by-entry-point): the hub reaches them through their manifests. The first is **telegram**, the hub's
Telegram channel (`docs/notifications.md`), which left the core as it was: its channel of kind `telegram`, its routes at
the paths they had (the empty `prefix`: its routers carry their own under `/v1`), its table `telegram_links` from
migration 0015. Nothing of it is per project, so it runs hub-wide: `evo-agents hub plugin list` and Administration,
Plugins show it as a core plugin installed with the image, and no project enables it.

### Builder

The second core plugin is **builder** (`evo_agents/core_plugins/builder`), which projects enable. It contributes the
agent type `builder`, whose profile is @builder, and the pack `builder/builder`. The pack holds the agent's
instructions and two skills. `plugin-authoring` covers the manifest, plugin/1, the iframe bridge of a page, signing,
and a CI template, `ci.yml`. `hub-settings` covers the proposals below. The type declares `mentioned_by: admin`, so
@builder takes work from the project's admins alone. A writer who mentions it gets the hub's refusal in the thread,
and no run starts. The pack's files come from the package: the hub packs them at claim time as the plugin's image
bundle and hands the worker that bundle, as it hands any pack.

**A change of a setting is a proposal.** The agent of a @builder chat run calls the MCP tool `builder__propose` of the
hub's /mcp with a kind, a target, the fields it sets and those it unsets, and a summary:

| kind | target | applies as | decided by |
| --- | --- | --- | --- |
| `branding` | the hub | the next version of the hub's branding | a hub admin |
| `features` | `{"layer": "hub"}` or `{"layer": "project"}` | the next version of that layer of features | a hub admin |
| `plugin_config` | `{"plugin": ...}`, with `"agent"` for an agent's layer | the next version of that layer of the plugin's config | an admin of the project |
| `agent` | `{"agent": handle}` | the next version of the profile (name, description, instructions, packs) | an admin of the project |

The hub answers the tool only for the agent of a @builder chat run that an admin of the project dispatched.
It then checks the change as the route that writes the setting would. @builder never sets a secret of a plugin's
config, never sets a branding image (it may unset one), and never changes the project's default agent. A change that
passes is kept pending in `builder_proposals` (migration 0043) at the setting's version. The hub shows it in the
thread, and each member who may decide it gets an Inbox notice of kind `builder_proposal` that links to that
message. Nothing changes until an admin approves it:

- in the thread, with `approve 12`, `reject 12` or `roll back 12`, which the hub reads through the event `chat.message`
  (a delivery made again does nothing new);
- with `GET /v1/x/builder/{project}/proposals[/{id}]`, `POST .../{id}/answer` `{"answer": "approve" | "reject"}`
  and `POST .../{id}/rollback`. These refuse a token scoped to a run.

An approved proposal is applied only while the setting is still at the version it was proposed at. Otherwise it
fails with the reason, and the agent proposes the change again. Applied, the setting has a new version, and a rollback
writes the next one as a copy of the version before it, while the setting is still at the version the proposal made.
The audit log keeps `builder.propose` and `builder.decide`.

**A change of code is the chat run itself.** The agent edits the client's plugin repo that the run checks out and
pushes its branch, and the hub opens the draft pull request (`docs/workers.md`). Nothing in it reaches the hub until
the repo's CI publishes a version. `ci.yml` extends the workflow of Signing in CI below with a third job, `publish`,
which runs after `sign` in the GitHub Environment `evo-hub`, whose required reviewers and secrets gate it. It writes a
hub admin's machine token from that environment's secrets, runs
`evo-agents hub plugin publish <bundle> --signature <bundle>.sigstore.json`, and deletes the token at the end. The
version waits in the store as available. A hub admin upgrades to it with the store's existing routes, and only then
does the hub install it.

The routes refuse a token scoped to a run, but they take an admin's own machine token like any other caller's. An
agent that can read such a token on its worker could approve its own proposal. Run @builder on workers where no
admin's machine token is readable by the agent.

## Runtimes

A plugin of either tier declares a runtime of agents with `acp`, the command of an agent that speaks the
[Agent Client Protocol](https://agentclientprotocol.com) on its stdin and stdout:

```json
"contributes": {
  "runtimes": [
    {
      "name": "gemini",
      "title": "Gemini CLI",
      "acp": {"command": ["gemini", "--experimental-acp"]},
      "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
      "secrets": ["GEMINI_API_KEY"]
    }
  ]
}
```

- `acp.command` is the program, looked for on the worker's `PATH` unless it is an absolute path, and its arguments: at
  most 32 items, each one line. The plugin does not ship the program; the worker's owner installs it.
- `models` (at most 200) are what the agent offers, which the worker reports for a dispatch to suggest; a run's model
  reaches the agent through its ACP config option of category `model`.
- `secrets` (at most 20, names such as `GEMINI_API_KEY`) are the variables the agent needs. The run's env leases give
  them (`docs/credentials.md`), or the worker's own environment; a run whose agent would lack one fails before the
  agent starts, with cause `credentials`.

A runtime names one of `acp` and `python`. A core plugin's `python` is the older form: a subclass of
`evo_agents.worker.adapter.Adapter` in the worker's process, the adapter of the runtime of the core its name gives.

**The hub registers a plugin's runtime as `<plugin>/<name>`** (`sandbox/gemini`) in each project that enabled the
plugin, while the plugin is installed and on, next to the core's `claude-code`, `opencode` and `codex`. A dispatch
(step runs, a plan run, a rerun) whose runtime the project does not register is a 422 that lists what it takes; schema
0039 holds a run's runtime only to that shape. A run asking for `any` takes a runtime of the core, never a plugin's. The
runtime axis of an agent profile names plugins' runtimes as it names the core's, and a profile that allows only
plugins' runtimes takes no run asking for `any`. A plugin that declares an ACP runtime is enabled per project, as an
isolated plugin always is; it needs no permission for it.

**A worker runs a plugin's runtime only once its owner accepts the plugin**, in the worker's plugins (its page, or
`PATCH /v1/workers/{id}`), and only with a daemon that says it runs plugins' runtimes. The hub then sends the daemon
the declaration with each heartbeat answer, the daemon looks for the program and reports the runtime, and a run of it
goes only to a worker that reports it available; another worker never claims it, and the run waits rather than fails.
The claim hands the worker the declaration as the project has the plugin then, and the worker runs the agent through
the core adapter of ACP (`docs/workers.md`, Runtimes of plugins): ACP version 1, the run's worktree as the session's
`cwd`, every permission the agent asks granted, no file system or terminal of the client, no MCP server. A run whose
plugin was disabled in its project before a worker claimed it fails there, saying so. Each run records the plugin's
version among the plugins of its project (`run_plugins`).

To check a runtime by hand, enable the plugin on a project, add it to a worker's plugins, wait for the worker's page
to list `<plugin>/<name>` under Runtimes, and dispatch to it:

```sh
evo-agents hub run dispatch rollout 2 --project evo-agents --runtime sandbox/gemini
```

## Environments

A plugin of either tier declares a run environment: a container image that a run's agent runs in on the worker,
instead of the worker's own machine (the host).

```json
"contributes": {
  "environments": [
    {
      "name": "box",
      "title": "Agent box",
      "image": "ghcr.io/acme/agent-box:1.2@sha256:0123...cdef",
      "network": "outbound",
      "mounts": [
        {"type": "tmpfs", "target": "/scratch", "size_mb": 256},
        {"type": "volume", "name": "npm-cache", "target": "/cache/npm"}
      ]
    }
  ]
}
```

- `image` is pinned by the digest of its manifest or index, `[registry/]repository[:tag]@sha256:<64 hex>`; a tag
  alone is refused, since it can move after the publisher signed the manifest. The worker pulls it by that digest
  before the agent starts and runs nothing else (`--pull=never`). The image holds the programs of the runtimes it runs
  (`claude`, `opencode`, `codex`, or an ACP agent's program), found on its own PATH.
- `network` is `none` (the container's loopback alone; opencode, whose server the daemon reaches over a port, cannot
  run there) or `outbound` (the engine's default network, out to the internet and the model's API, with nothing
  published but the port an agent serves the daemon on, bound to the worker's 127.0.0.1).
- `mounts` (at most 8, each target once) are `tmpfs`, an empty file system in memory with an optional `size_mb`, or
  `volume`, a named volume of the engine that outlives the run, such as a cache, which the worker names
  `evo-<plugin>-<name>` so plugins never share one; `read_only` mounts either read-only. A target is a normalized
  absolute path without a comma or a colon, never `/`, `/proc`, `/sys`, `/dev` or a path under them. A plugin never
  mounts a path of the worker's own: the worker mounts the run's worktree, the git directory of each checkout it
  belongs to and a home of the run itself, and refuses a declared target that is or holds one of its paths.

**The hub registers a plugin's environment as `<plugin>/<name>`** (`sandbox/box`) in each project that enabled the
plugin, while the plugin is installed and on, next to `host`. A plugin that declares one is enabled per project and
needs no permission for it. **The eighth axis of an agent profile, environment, chooses where its runs' agent runs**:
`all` runs it on the host, as every run ran before the axis; an allowlist names `host` and environments of plugins in
order of preference. A dispatch (step runs, a plan run, a rerun, an author run) under a profile whose allowlist names
no environment its project registers is a 422 that lists what the project registers.

**A worker runs a plugin's environment only once its owner accepts the plugin**, in the worker's plugins (the same
list as for runtimes), and only with a daemon that says the feature `environments`. A run whose profile allows only
plugins' environments goes to such a worker alone; another never claims it, and the run waits rather than fails. The
claim picks the first environment of the allowlist that is `host` or one the worker takes and the run's project
registers, records it in the run (`environment`, null for the host, shown by `evo-agents hub run show --json`), and
hands the worker its declaration as the project has the plugin then. A run whose allowlist names no environment its
project still registers when a worker claims it fails there, saying so. A dispatch pinned to a worker that could never
take one is 409.

The worker's container provider runs the agent through Docker or Podman (`docs/workers.md`, Environments of plugins):
as the owner's user, with the run's worktree mounted at its own path, every capability dropped, and only the variables
the run's leases and the run itself set passed in, never the worker's own environment. Its preflight checks the
engine and pulls the image; a worker without an engine, or whose engine cannot pull the image, fails the run before
its agent starts, with cause `environment`. A container takes step and task runs: the agent of a plan, author, chat,
review or judge run talks to the daemon through `evo-agents worker`, which a container does not have.

To try one by hand, enable the plugin on a project, add it to a worker's plugins, give an agent the axis, and dispatch
under it:

```sh
evo-agents hub agent create @boxed --project evo-agents --name Boxed --allow environment:sandbox/box
evo-agents hub run dispatch rollout 2 --project evo-agents --agent boxed
```

## Agent packs

A plugin contributes agent packs: what an agent profile adds to its runs' agent besides its own instructions. A pack
is the directory `packs/<name>/` of the bundle, laid out as a Claude Code plugin, and the manifest declares it by name:

```json
"contributes": {
  "agent_packs": [
    {"name": "reviewer", "title": "Reviewer kit", "description": "Review skills, a lint hook and the tracker's MCP server."}
  ]
}
```

```text
packs/reviewer/
  skills/review/SKILL.md     a skill: front matter with name (its directory's) and description
  agents/critic.md           a subagent
  hooks/hooks.json           hooks: an object whose "hooks" is an object
  .mcp.json                  MCP servers: "mcpServers", each stdio (command, args, env) or remote (type http or sse, url, headers)
  INSTRUCTIONS.md            text the agent's system prompt gains, at most 32 KiB
  scripts/lint.sh            any other file: a resource a hook or a skill runs
```

`${CLAUDE_PLUGIN_ROOT}` in `.mcp.json` and `hooks.json` stands for the directory the worker builds the pack in. A
pack holds at most 64 skills, 64 subagents and 20 MCP servers. What a Claude Code plugin may hold and no runtime of
the worker is handed is refused: `commands/` (skills replace them), `output-styles/`, `.lsp.json`, `settings.json`
and a `.claude-plugin/` directory, since the worker writes the plugin's manifest itself. `evo-agents plugin pack`
(and so `plugin test` and `plugin sign`) reads every pack the manifest declares, and the hub refuses a bundle whose
pack does not read when it is published, before any profile attaches it (`evo_agents/plugins/agent_packs.py`).

**The hub registers a pack as `<plugin>/<name>`** (`sandbox/reviewer`) in each project that enabled the plugin, while
the plugin is installed and on; a plugin that contributes one is enabled per project. `GET
/v1/projects/{project}/agent-packs` lists them, each with the version installed now and every version the hub holds
that declares the pack and runs on this release, the newest first.

**A profile attaches packs pinned at a version of their plugin** (`docs/hub.md`, Agent profiles): at most 16, each
once, at the version given or the one installed now. The pin holds when the plugin moves on; only an edit of the
profile moves it, and that edit is a new version of the profile. A pack it attaches counts as named on its skill and
tool axes: its skills pass the skill axis and its MCP servers the tool axis (`mcp:<server>`), since pinning the pack
at a version is the admin's consent to what that version holds. The axes still limit everything else, and a pack
widens no other axis. The project's default profile attaches none.

**A worker builds packs only once its owner accepts the plugin**, in the worker's plugins (the same list as for
runtimes and environments), and only with a daemon that says the feature `agent_packs`; another never claims the
run. The worker builds each pack into the run's directory before the agent starts and hands it to the runtime in its
own form (`docs/workers.md`, Agent packs): Claude Code loads it as a plugin, with every kind of component; opencode and
Codex get its skills, MCP servers and instructions, and refuse a pack with subagents or hooks; the runtimes of plugins
(ACP) take no pack. A pack the run's runtime cannot load fails the run before its agent starts, saying what.

To try one, enable the plugin on a project, add it to a worker's plugins, attach the pack to an agent, and dispatch
under it:

```sh
evo-agents hub agent create @reviewer --project evo-agents --name Reviewer --pack sandbox/reviewer@1.2.0
evo-agents hub run dispatch rollout 2 --project evo-agents --agent reviewer
```

## Agent types

A plugin contributes **agent types** (`contributes.agent_types`): each a template of an agent profile with the parts of
the same plugin its runs use. The plugin then hands that agent tasks, and each task runs as a run of kind `task` on a
worker of the project (`evo_agents/hub/task_runs.py`):

```json
"contributes": {
  "agent_packs": [{"name": "triage", "title": "Triage kit"}],
  "environments": [{"name": "node", "title": "Node 22", "image": "ghcr.io/acme/node@sha256:..."}],
  "jobs": [{"name": "tick", "cron": "*/10 * * * *"}],
  "events": [{"name": "results", "on": ["task.result"]}],
  "agent_types": [
    {
      "name": "triager",
      "title": "Issue triager",
      "profile": {"handle": "triager", "name": "Triager", "instructions": "Reproduce, then fix or explain."},
      "packs": ["triage"],
      "environment": "node",
      "jobs": ["tick"],
      "events": ["results"]
    }
  ]
}
```

`profile` takes the handle, name, description, instructions and permissions (by axis) of an agent profile
(`docs/hub.md`, Agent profiles). `packs`, `environment`, `jobs`, `events` and `pages` name the plugin's own
contributions by name. A manifest that names a part the plugin does not contribute, or one part twice, is refused, as
are two types with the same handle. `mentioned_by` says who hands the profile work by mentioning it in a group chat:
`writer`, the default, for the project's writers, or `admin` for its admins alone. Any other member who mentions it
gets the hub's refusal in the thread, and no run starts (core plugin builder, above).

**An admin of a project enables an agent type there** after enabling its plugin:
`PUT /v1/projects/{project}/agent-types/{plugin}/{name}` with:

- `run_as`: the member whose name its runs go under, a writer or an admin of the project;
- `workers`: the workers of that member its runs may take, by name, at most 16 (none: any the profile allows);
- `daily_usd`: the cap of what its task runs may cost in 24 hours, above 0 and at most 1000.

The first enable creates the type's profile in the project from the template:

- its packs attached at the version of the plugin the project runs;
- its environment, when it names one, as the only one its runs may take.

A handle another agent of the project holds, deleted ones included, refuses the enable (409), as does a plugin the
project did not enable or turned off. Enabling the type again keeps that profile, which the project's admins edit like
any other, and changes `run_as`, `workers` and `daily_usd`. A deleted profile refuses it (409): a handle is never
taken again. `DELETE` on the same path turns the type off and keeps its row and profile. `GET
/v1/projects/{project}/agent-types` lists the types of the plugins the project enabled, to every member, each with how
it is enabled and what its task runs cost in the last 24 hours. The audit log keeps `agent-type.enable` and
`agent-type.disable`.

**A plugin granted `runs:dispatch` hands a task to one of its own agent types** through the hub's API for plugins,
with the token the plugin host hands its backend (`Authorization: Bearer evp_...`) and no other credential:

```http
POST /v1/plugin-api/projects/evo-agents/agent-types/triager/tasks
Authorization: Bearer evp_...
Content-Type: application/json

{"task": "Issue #212: the export drops the last row. Reproduce it and fix it.", "title": "Fix #212"}
```

`task` is at most 16 KiB in UTF-8 and `title` at most 200 characters (when left out, the task's text on one line, cut short). The hub
refuses the dispatch with:

- 401 without a live plugin token, or when the plugin is uninstalled or turned off;
- 403 when the project did not enable the plugin or did not grant it `runs:dispatch`;
- 404 for a type the installed version does not contribute, or one the project did not enable or turned off;
- 409 when the type's profile was deleted, or `run_as` no longer holds writer on the project;
- 422 for a body it does not take;
- 429 once the type's task runs cost its cap in the last 24 hours, as their usage says.

Otherwise it answers 201 with the task: `task_id`, the id of the run it queued, and `budget`, what is left of the cap
as the run's cost cap with 30 minutes of agent time, as a chat run has. A runtime that reports no cost stops at the
time. The run goes under the type's profile, dispatched by `run_as` with the credential kind `machine`, and waits for
a worker of `run_as` that:

- the type names (any, when it names none);
- runs task runs;
- has a checkout of one of the project's repos, when the project has any;
- passes the profile's axes.

The audit log keeps `task.dispatch`, with `run_as` as its actor.

**A task run changes code on the branch `task/<task_id>` of each repo** of the project its worker has a checkout of, in
one headless turn of the agent, which asks nobody anything (`docs/workers.md`, Task runs). At its end the worker:

- commits what the agent left;
- pushes that branch of each repo it changed, never a default branch and never forced;
- tells the hub of each push.

The hub opens a draft pull request of the branch with the workers' GitHub App, or finds the one open, and never merges
it. A repo elsewhere than GitHub, or a hub without the App, gets the pushed branch only. A run its worker lost goes on
in its next attempt on the same branch. A task run is never rerun: the plugin hands the task again (409).

**The plugin learns the end through its events**: when the run is done, failed or cancelled, the hub writes the event
`task.result` for the plugin of the agent type alone, while the project grants it `runs:dispatch`. Its payload holds:

- `task_id`, `run_id`, `attempt`, `project`, `agent_type` and `state`;
- `branch`;
- `pulls`, the draft pull requests as `{repo, url, number}` each;
- `failure_cause`.

`GET /v1/plugin-api/projects/{project}/tasks/{task_id}` reads the same of a task's latest attempt, with when it was
queued and when it ended. It answers for the plugin's own tasks only (404 for any other).

## Pages

A plugin adds pages to the part of the web of a project that enabled it, at `/p/<project>/x/<id>/<name>`, each with
an item in the project's menu after the project's own areas (`menu: false` leaves it out; a link then reaches it), its
`title` (60 characters; the plugin's name when left out) and an `icon` from the hub's set: `puzzle` (the default),
`file-text`, `list-checks`, `layout-dashboard`, `table`, `kanban`, `book-open`, `chart-column`, `calendar`,
`clipboard-list`, `notebook-pen`, `bug`, `folder-kanban`, `message-square`, `gauge`, `shield-check`, `workflow` and
`boxes`. A plugin never brings an icon, a script or a style of its own into the hub's web. In a project that has not
enabled the plugin, where the hub turned it off, or for a page it lacks, the page is a 404, as its routes are. A plugin
with a page is enabled per project.

**A core plugin's page is native**, a page of the hub's web itself: a feature folder of its own under
`web/src/components` (the web's ESLint rules keep it from importing another feature), a route under
`web/src/app/(app)/p/[project]/x/<id>/` whose layout calls `requirePlugin` (`web/src/lib/plugins-server.ts`) so the
page answers 404 where the plugin is not on, and the plugin's own routes for its data. Its manifest names no `entry`.
The web of the image carries it; web/DESIGN.md, section Plugins, says how to add one. The tests' core plugin `notes`
(`tests/hub/core_notes.py`) has one, which only the web's end-to-end build carries (`EVO_WEB_FIXTURES=1`).

**An isolated plugin's page is an HTML file of its bundle under `pages/`** (`entry`, such as `pages/board.html`, which
the bundle must hold: `evo-agents plugin pack` and the store refuse it otherwise). The page loads its scripts, styles,
images and fonts from the bundle's `pages/` by relative URL, and may write them inline. The web frames it:

- **The frame.** `<iframe sandbox="allow-scripts allow-forms">`, never `allow-same-origin`: the page's document has an
  opaque origin, so it reads no cookie or storage of the hub (`document.cookie` throws `SecurityError`), cannot reach
  the page around it, and its own requests carry no session. It cannot open popups or navigate the hub's page.
- **The files.** `GET /v1/plugin-ui/<id>/<project>/<expires>.<mac>/<file>` serves the files of the installed version's
  `pages/` (HTML, JavaScript, CSS, JSON, text, SVG, PNG, JPEG, GIF, WebP, icons and web fonts; anything else is a 404).
  The frame sends no cookie, so the path's ticket is the credential: an HMAC of the plugin, the project, the SHA-256 of
  the installed bundle and the expiry under the hub's session secret (with the purpose `plugin-ui`, as the hub's other
  signed values), handed out in `pages[].url` of `GET /v1/projects/{p}/plugins` to the project's members, and the same
  for an hour so a page read twice frames one URL; it lasts 12 hours past that hour, and only while that bundle is
  installed: after an upgrade the tickets of the version before serve nothing. Each file is checked again as it is
  served (installed, turned on, enabled in that project), and anything else, a wrong or old ticket included, is the
  same 404. Every file answers with
  `default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:;
  font-src 'self'; media-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none';
  frame-src 'none'; worker-src 'none'; frame-ancestors 'self'; sandbox allow-scripts allow-forms`, `nosniff` and no
  referrer: the page fetches nothing itself, only the hub's web frames it, and opened on its own it is still a sandbox.
- **The bridge `evo-plugin/1`.** The page posts requests to its parent, and the web answers each by calling the hub
  with the viewer's session, so the hub checks the viewer's grant as it does for the web itself, on top of the
  plugin's. A request is `{"bridge": "evo-plugin/1", "id": <string or number>, "method": "...", "params": {...}}`
  (params 64 KiB at most as JSON); the answer is `{"bridge", "id", "result"}` or `{"bridge", "id", "error": {"code",
  "message", "status"}}`, posted to the frame's window only. The web answers only messages from the frame's own window
  with an opaque origin, 16 at a time, and posts `{"bridge", "event": "theme", "theme": "light" | "dark"}` when the
  viewer's theme changes. A method opens only when the project grants the plugin the permission it needs now: before
  such a call the web reads the plugin's grant again when its last read is over 5 seconds old, and refuses the call
  with `not_granted`, never reaching the hub, once the plugin lacks it or is no longer on in the project. Every call
  the bridge makes names the plugin in the header `X-Evo-Plugin`, and the hub checks it again as it arrives
  (`evo_agents.hub.server.plugin_bridge`): such a call reaches only the reads below and the plugin's own routes, and a
  read only while the plugin is installed, turned on, enabled in the project, with the permission in what the project
  granted that the installed version still asks for; anything else is a 403, which the page gets as `hub_refused`. A
  page left open after its plugin was disabled, removed, turned off or upgraded to ask for less gets nothing more:

  | Method | Needs | Params | Result |
  | --- | --- | --- | --- |
  | `context` | | | the bridge, project, plugin, page, viewer (`login`, `role`), locale, theme, and the methods open to the page |
  | `resize` | | `height` (CSS px) | the frame's new height, from 240 to 4000 px |
  | `route` | | `method` (GET by default), `path` below the plugin's routes, `query`, `body` (JSON, 1 MiB) | `{status, body}` of `/v1/x/<id>/<project>/<path>`, the plugin's own route, with the viewer's session (and its CSRF header for a write) |
  | `runs.list` | `runs:read` | `state` (states of runs), `limit` (1 to 50), `offset` | the project's runs, as `GET /v1/projects/{p}/runs` |
  | `runs.get` | `runs:read` | `id` | one run |
  | `plans.list` | `plans:read` | `area` (`active` or `completed`) | the project's plans |
  | `plans.get` | `plans:read` | `id` | one plan |
  | `memories.list` | `memories:read` | `limit` (1 to 100), `cursor` | the project's memories |

  Errors: `unsupported_bridge` (another version), `invalid_request`, `unknown_method`, `not_granted`,
  `invalid_params`, `too_large`, `busy`, `hub_refused` (the hub's refusal, with its status and message) and
  `hub_unavailable`. `chats:*`, `runs:dispatch` and `notifications:send` open no method of the bridge: they serve the
  plugin's backend.

A minimal page:

```html
<!doctype html>
<main id="board">Loading</main>
<script>
  const pending = new Map();
  window.addEventListener("message", (event) => {
    const { bridge, id, result, error } = event.data ?? {};
    if (event.source !== window.parent || bridge !== "evo-plugin/1" || !pending.has(id)) return;
    const [resolve, reject] = pending.get(id);
    pending.delete(id);
    error ? reject(error) : resolve(result);
  });
  let next = 0;
  const call = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++next;
      pending.set(id, [resolve, reject]);
      window.parent.postMessage({ bridge: "evo-plugin/1", id, method, params }, "*");
    });
  call("runs.list", { limit: 5 }).then((list) => {
    document.getElementById("board").textContent = `${list.total} runs`;
    return call("resize", { height: document.body.scrollHeight });
  });
</script>
```

The sample plugin of the tests (`tests/fixtures/plugins/sample/pages`) does the same and records what its sandbox
refuses.

## Writing and checking a plugin

The author builds and checks a plugin on their own machine with `evo-agents plugin`:

```sh
evo-agents plugin test  ./my-plugin           # the conformance checks of plugin/1 (below); exit 1 when one fails
evo-agents plugin pack  ./my-plugin           # the deterministic bundle, <id>-<version>.tar.gz, and its SHA-256
evo-agents plugin keygen --output key.b64 --publisher acme   # an Ed25519 key, and its EVO_HUB_PLUGIN_PUBLISHERS entry
evo-agents plugin sign  my-plugin-1.0.0.tar.gz --key key.b64 # writes my-plugin-1.0.0.tar.gz.sig.json
```

`plugin test` runs the same checks a hub relies on, as `evo-agents kg connector test` does for a connector of kg/1:
the manifest reads; the directory packs into a bundle; an isolated plugin that serves a point declares a backend whose
files are in the bundle; its virtual environment builds; started with an environment built from nothing it answers
`initialize` naming plugin/1 and listing exactly the MCP tools the manifest contributes; it answers `ping`; each
contribution answers its call with a result that fits the method's schema; an unknown method gets `-32601` and the
backend serves on; a line that is not JSON gets `-32700` with id null, or nothing, and the backend serves on; nothing
but plugin/1 responses appear on stdout; and it answers `shutdown` and exits. The sample plugin
`tests/fixtures/plugins/sample` passes every check.

### Signing in CI

A publisher signs keyless in the CI of the plugin's repository: no key exists to leak, and the operator trusts a
workflow rather than a person (`docs/hub.md`, section Plugins). A workflow that does it has two jobs:

- **test** installs evo-ak, runs the plugin's own tests and `evo-agents plugin test`, packs the bundle, and passes on
  its file name and SHA-256;
- **sign** runs on the default branch only and is the only job with `id-token: write`. It runs none of the plugin's
  code: it packs again (packing is deterministic), fails unless the SHA-256 is the one the test job packed, signs with
  `python -m sigstore sign`, which writes `<bundle>.sigstore.json` beside the bundle, and keeps both as an artifact. A
  hub admin publishes them with `evo-agents hub plugin publish <bundle>`.

```yaml
  sign:
    needs: test
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write # the OIDC token Sigstore's Fulcio turns into this workflow's signing certificate
    env:
      BUNDLE: ${{ needs.test.outputs.bundle }}
      TESTED: ${{ needs.test.outputs.sha256 }}
    steps:
      - uses: actions/checkout@<commit sha>
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@<commit sha> # a standalone Python, on a self-hosted runner too (below)
      - run: uv venv --seed --python 3.12 "$RUNNER_TEMP/venv" && echo "$RUNNER_TEMP/venv/bin" >> "$GITHUB_PATH"
      - run: python -m pip install evo-ak "sigstore>=4.5,<5" # evo-ak of a release that has `evo-agents plugin`
      - run: mkdir dist && (cd dist && evo-agents plugin pack ../my-plugin)
      - run: test "$(sha256sum "dist/$BUNDLE" | cut -d' ' -f1)" = "$TESTED"
      - run: python -m sigstore sign "dist/$BUNDLE"
      - uses: actions/upload-artifact@<commit sha>
        with:
          name: my-plugin-bundle
          path: dist/
```

The operator's entry for that publisher names the workflow, `acme=sigstore:acme/my-plugin/.github/workflows/ci.yml`,
which the workflow reads as `${GITHUB_WORKFLOW_REF%%@*}`. The hub accepts a signature from any ref of that workflow,
so signing on the default branch alone is the workflow's own rule. Keyless signing writes the certificate to
Sigstore's public transparency log (Rekor): the repository's name, the workflow's path, the ref, the commit and the
run become public there, even for a private repository. A publisher that must keep those private signs with an
Ed25519 key instead (`plugin keygen`, `plugin sign`), and the operator trusts `acme=ed25519:<public key>`.

**On a self-hosted runner**, take Python from uv (`astral-sh/setup-uv`, then `uv venv --seed --python 3.12`) or
another standalone build, not from `actions/setup-python`. The plugin host builds a backend's environment from
nothing, so it has no `LD_LIBRARY_PATH`, and the Python that setup-python installs on a self-hosted runner finds its
`libpython` only through that variable: `plugin test` fails at the environment check. Making the host pass the
variable on would open the environment it builds from nothing, so the runner adapts instead.
