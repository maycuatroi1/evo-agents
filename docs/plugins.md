# Plugins: the plugin/1 protocol and the plugin host

This is the reference for the backend of an isolated plugin: the protocol it speaks, where it runs, and how its author
checks it. The store, the manifest, signing, permissions and config are in `docs/hub.md`, section Plugins; this file is
about what runs the code.

A plugin is core or isolated (`tier` in the manifest). A **core** plugin is a Python package in the hub's image; its
contributions run in the api and the worker processes. An **isolated** plugin, the default, carries a **backend**: a
program the hub runs outside its own processes, in a plugin host, and reaches over the `plugin/1` protocol. An isolated
plugin never runs Python in the hub; it serves its routes, MCP tools, jobs, events and channels from its backend, and
its pages run in a sandboxed iframe (step 7).

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
  naming the contribution and the project (step 5 wires these to the hub). Their params are open objects, so a later
  minor version of plugin/1 adds keys without a new major.
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
CPUs, memory and processes (`EVO_HUB_PLUGINS_CPUS`, `EVO_HUB_PLUGINS_MEMORY`, `EVO_HUB_PLUGINS_PIDS`).

**Each backend runs under a user of its own**, `UID_BASE` (200000) plus the plugin's id on the hub, with a group of
the same number and no other group. The host gives one only as root, and a host that is not root **refuses to start
a backend** (its state says why), unless it is started with `--allow-shared-user` on a developer's machine. Under a
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
of the current directory runs) under the backend's user: it changes into the data directory and sets the resource
limits before any code of the plugin runs, then execs the environment's Python (`-I`). The limits are no core file,
1024 open files, 4 GiB of address space per process and 256 processes and threads for the plugin's user. Then a
**launcher** marks the backend **not dumpable** (`prctl(PR_SET_DUMPABLE, 0)`) and runs the entry; the host marks itself
the same way at start. Stopping a backend kills its process group and then every process of its user (`kill(-1)` from
a process of that user, until `/proc` shows none), so a child that left the group with `setsid()` goes too. Placing
runs in a thread that cancelling a start does not end, so stopping also kills pip and waits for that thread, and the
backend that replaces one, under the same user, starts only then. A start that cannot kill every process of the
plugin's user places nothing and starts nothing: it backs off.

The doors a secret could leave by, and what shuts each:

| Door | Shut by |
| --- | --- |
| the backend's environment and arguments | built from nothing; secrets only on stdin |
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
process of its user behind.

### The hub token of a backend

The hub mints a backend's token when the host is about to start it, and only then: the host pulls the plugins and
their payloads from `GET /internal/plugin-host/plugins`, which mints nothing, and asks
`POST /internal/plugin-host/plugins/{name}/token` just before each start, which revokes the plugin's older tokens.
A pull that finds nothing changed restarts nothing. Removing a plugin, or the hub turning it off, revokes its tokens at
once, and the host stops its backend at its next pull (every 60 seconds). What a token may do on each request is the
extension points' check (step 5).

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

For signing with Sigstore from CI (the way an operator trusts without a shared key), see `docs/hub.md`, section
Plugins; the sandbox plugin repository of step 8 is the worked example.
