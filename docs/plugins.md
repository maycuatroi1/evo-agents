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
- `requirements` is an optional pip requirements file inside the bundle; the host installs it into the plugin's own
  virtual environment from wheels only, so installing runs none of the plugin's code.

The paths stay inside the bundle. A core plugin has no backend; an isolated plugin that only contributes pages and
config may omit it.

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
compose file it is the `plugins` service, built from the same image.

For each plugin at a version, under the host's data directory:

- `code/<name>/<version>/` holds the bundle's files, written once and read-only to the backend;
- `envs/<name>/<version>/` is its own virtual environment, built with `python -m venv --without-pip` and the
  requirements installed from wheels;
- `data/<name>/` is the one directory the backend writes, its `HOME` and, under `tmp`, its `TMPDIR`.

The host builds each backend's environment **from nothing** (`evo_agents.plugins.environment.backend_env`): `PATH`,
`HOME`, `TMPDIR`, `LANG`, `VIRTUAL_ENV` and the plugin's name, version and protocol, and no more. Nothing of the
host's environment reaches a backend, so even a host started by mistake with a hub variable does not pass it on. A
backend's secrets arrive only in `initialize`, on its stdin, and are its own plugin's: the hub decrypts the secret
config fields where the key is and never puts another plugin's secret in a backend's payload.

On Linux a launcher marks each backend **not dumpable** (`prctl(PR_SET_DUMPABLE, 0)`) before it runs the entry, and
the host marks itself the same way, so a process of the same user cannot read another's `/proc` (environ, memory) or
attach to it: one plugin cannot read another's secrets, nor the host's shared token, through `/proc`. When the host
runs as root (the compose service, with only the capabilities SETUID, SETGID, CHOWN and KILL), each backend runs under
a **user of its own** (`UID_BASE` plus the plugin's id), so a plugin cannot read another's files either; run as any
other user, the host starts every backend under its own user, which still keeps their environments, arguments, memory
and secrets apart, but not their files, so a secret never touches disk. `evo_agents.plugins.environment.isolation`
says which mode is in force (`uid` or `process`).

A secret of the hub reaches a backend only by two doors, and both are shut: the environment (built from nothing) and
`/proc` of the api, the worker, the host or another backend (not dumpable, or a different user). A hostile plugin that
tries every door and reports what it reads is `tests/fixtures/plugins/hostile`, and `tests/plugins/test_plugin_isolation.py`
proves it reads nothing.

## Crashes, hangs and restarts

The api calls the host with a timeout of its own, so a backend that hangs never holds an api request open past it: the
host (`evo_agents.plugins.host`) fails the call with `BackendUnavailable` and the api answers a 503. A backend that
crashes or hangs is stopped and started again after a backoff (1 s, 2 s, 5 s, 15 s, 30 s, then 60 s), its process
group killed so nothing it spawned lingers. The host reports each backend's state to the hub, which a hub admin reads:

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
