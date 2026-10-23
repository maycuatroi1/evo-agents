# Browser for agents

A run dispatched with a browser gets a real Chromium that the worker's daemon builds and starts for it (see
[workers](workers.md)). A login is kept as a **browser identity**: a Chromium profile held by one worker, sealed on the
hub, with the sites (domains) whose cookies it keeps. The dispatcher sees and drives the run's browser on the run page of
the hub.

## Identities

| Owner | Who makes it | Who may use it |
| --- | --- | --- |
| `member` | a writer of the project | its owner, for runs they dispatch |
| `project` | an admin | every writer of the project |
| `agent` | an admin, for an agent profile (`--agent HANDLE`) | the runs under that profile |

- An identity is held by one worker (`worker`), which a login session or a local login may change. Only a run on that
  worker takes it; a run that names it is pinned there. A run holds it while it runs (`held_by_run`); no second run and no
  local login takes it meanwhile.
- The profile is sealed per version (AES-256-GCM, data key sealed under `EVO_HUB_SECRETS_KEY`) and the hub keeps two
  versions. Every save removes cookies outside the identity's domains first. No answer carries a cookie or a key.
- Status: `active`, `needs_login` (the profile could not be used; log it in again) or `disabled`.

### Identities of an agent profile

`hub browser identity create NAME --owner agent --agent HANDLE --domain HOST --worker W` makes one for a profile. The
first of a profile is its default, and `POST .../browser-identities/{id}/default` changes which one is. A dispatch under
the profile with no `--identity` gets the default identity and a browser, pinned to the identity's worker (422 when the
dispatch pins another worker). Reruns keep it. A dispatcher cannot name an agent's identity, and members who are not
admins do not see them. Deleting the profile retires its identities (409 while a run holds one).

## Logging in

1. **On the web**: `hub browser identity login NAME` (or the Browser page) queues a login session, a run of kind `browser`.
   Open it, sign in through the live view and press Save; cancelling saves nothing.
2. **On the worker's machine**: `evo-agents worker browser login NAME --project P [--owner agent --agent H] --domain HOST`
   opens a Chromium window on that screen with no run. The identity is made, pinned to this worker, when it does not
   exist (it needs `--domain`). Sign in and close the window: a Chromium with no window deletes the cookies outside the
   domains and the profile goes to the hub as the next version. The worker's owner needs the writer role (admin for an
   agent's identity). A stale version, a held identity or a disabled one saves nothing.
3. **Inside a run**: `evo-agents worker browser login --identity NAME --url URL --reason TEXT` asks the dispatcher to sign
   in, as a decision of category `login`.

## Dispatching

`hub run dispatch --browser` gives a Chromium with no login; `--identity NAME` (repeatable) gives one with those
identities, and the web dispatch dialog has the same choice in its Browser field. See the browser pack in
[workers](workers.md#the-browser-pack-and-the-login-decision).
