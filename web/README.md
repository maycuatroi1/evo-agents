# evo-agents hub web

The web interface of the hub: Next.js (App Router, TypeScript strict), Tailwind and shadcn/ui, TanStack Query
and Table, next-intl (English by default, Vietnamese from the user menu). It reads the hub API only through a
client generated from the API's OpenAPI document, and writes nothing except the admin actions and sign-out.
`DESIGN.md` describes the design system.

In production it is its own image (`output: "standalone"`, built by `web/Dockerfile`) behind the same domain as
the API: the reverse proxy sends `/v1` and `/mcp` to the API and everything else here. It never ships in the Python
package. `docs/hub.md` covers the deployment.

## Running it

Node 22 and pnpm (the version pinned in `package.json`), plus a hub API to talk to.

```sh
pnpm install
EVO_HUB_API_INTERNAL_URL=http://127.0.0.1:8080 pnpm dev
```

| Variable | Read | Meaning |
| --- | --- | --- |
| `EVO_HUB_API_INTERNAL_URL` | runtime, and at build time for the rewrite | where server components reach the API; when set at build time, `/v1/*` is also forwarded there so the browser stays on one origin (local runs, Playwright, the image: `web/Dockerfile` builds with `http://api:8080` unless given `--build-arg EVO_HUB_API_INTERNAL_URL=...`) |
| `EVO_HUB_WEB_TIME_ZONE` | runtime | time zone of dates rendered on the server, `Asia/Ho_Chi_Minh` by default |
| `PORT`, `HOSTNAME` | runtime | where `pnpm start` (the standalone server) listens |
| `EVO_WEB_FIXTURES` | build time | `1` builds the routes of the tests' fixtures too (`page.fixture.tsx`, `layout.fixture.tsx`: the native page of the core plugin notes); Playwright's build sets it, the image never does |

Sign-in is GitHub's web flow on the API (`/v1/auth/web/login`), so the API needs
`EVO_HUB_PUBLIC_URL` set to the address the browser uses for the web, plus the client secret and session
secret. The session cookie is the API's, httpOnly; server components forward it to the API and keep nothing.

## Scripts

| Command | What it does |
| --- | --- |
| `pnpm lint` | ESLint, no warnings allowed, then `scripts/check-structure.mjs` (not run by CI). Together they hold `src/` to files of 400 lines and imports in one direction, with no cycle; `eslint/structure.mjs` has the rules and a baseline that may only shrink |
| `pnpm typecheck` | route types, then `tsc --noEmit` |
| `pnpm test` | Vitest unit tests |
| `pnpm gen:api` | regenerates `src/lib/api/schema.d.ts` from `evo-agents hub openapi` (this checkout's Python; set `PYTHON` to choose the interpreter) |
| `pnpm build`, `pnpm start` | production build, then the standalone server |
| `node scripts/bundle-budget.mjs` | after `pnpm build`: First Load JS of each route against `bundle-budget.json`; `--chunks <route>` lists a route's chunks, largest first |
| `pnpm exec playwright test` | end-to-end tests, see below |
| `pnpm screenshots` | the README's images, from a made-up demo, into `docs/images/` at the repo's root; see Screenshots below |

CI fails when `schema.d.ts` differs from what `pnpm gen:api` writes. After changing an API model, run it and
commit the result. The document holds the routes of the core plugins the hub's image installs, Telegram's among them,
so that Python has them too, installed after evo-ak:
`uv pip install --no-deps --require-hashes -r deploy/hub/core-plugins.txt` (`docs/plugins.md`, section Core plugins).
The end-to-end stack needs them as well, for `e2e/telegram.spec.ts`.

CI also fails when a route loads more JS first than its budget in `bundle-budget.json`: the gzip size of the chunks
the build lists for the route, in kB of 1000 bytes. Load the heavy part later (`next/dynamic`), or raise the budget
and say why in the entry's `"reason"`; a budget raised or a route added over main's file without a reason fails too.
`node scripts/bundle-budget.mjs --write` sets every budget to what the build measures and keeps the reasons.

## End-to-end tests

Playwright starts two servers: `e2e/hub_stack.py` (a database of its own on the test Postgres, a fake GitHub,
moto's S3 as the blob store, `evo-agents hub serve` from this checkout) and the web's production build. Tests
sign in through the real web flow against the fake GitHub and seed projects, grants, memories and skills through
the API; a skill bundle is downloaded from the moto bucket the way a browser follows a presigned URL, and the
knowledge graph specs push a small synthetic graph and have the stack build it (`e2e/kg_seed.py`). The plugin specs
publish the sample plugin of `tests/fixtures/plugins/sample`, which the stack signs with a key the hub trusts, and the
hub runs through `e2e/hub_serve.py`, which puts the tests' core plugin notes in its image.

```sh
pnpm exec playwright install chromium firefox
EVO_HUB_TEST_DSN=postgresql://postgres:test@localhost:55433/postgres pnpm exec playwright test
```

Every spec runs in Chromium; `e2e/terminal.spec.ts` also runs in Firefox (the `firefox` project), since the run page's
terminal depends on how each browser applies the CSP to its websocket and to WebAssembly.

`EVO_HUB_TEST_DSN` names a superuser on a throwaway Postgres; the stack creates `evo_hub_e2e_<random>` and
drops it when Playwright stops. Ports default to 3324 (web), 18324 (API) and 18325 (stack control); change
`E2E_WEB_PORT`, `E2E_API_PORT` and `E2E_STACK_PORT` to run two checkouts at once, and set
`E2E_REUSE_SERVERS=1` to reuse servers that are already up. The API log is `e2e/.stack/hub-serve.log`.

CI runs the specs in six shards (`pnpm exec playwright test --shard=<i>/6`), each with a Postgres, a stack and a
production build of its own; a failed shard uploads its report as `playwright-report-<i>`. A pull request that
touches neither `web/` nor the Python package skips them, and one that touches only `web/` skips the Python tests.

`E2E_SCREENSHOT_DIR=<dir>` turns on `e2e/screenshots.spec.ts`, `e2e/memories-skills-screenshots.spec.ts`,
`e2e/kg-screenshots.spec.ts`, `e2e/runs-screenshots.spec.ts`, `e2e/run-outputs-screenshots.spec.ts`,
`e2e/insights-screenshots.spec.ts` and `e2e/workers-screenshots.spec.ts`, which write review screenshots of the shell,
the memories, skills, knowledge graph, runs, a run's files (its Trace and Outputs tab, the viewer, a question, the
Inbox and the project's card), Insights and workers pages (a run's page and its diff, the Run plan dialog, a plan run's banner and page, a
worker's Version card and its update notes included) in light and dark (the memories, skills, runs, Insights and workers
ones also at 375 px).

The runs specs are the worker themselves: they claim runs, report states and send events, messages and diffs with a
worker token, as the daemon does (`e2e/support/runs.ts`), and `e2e/run-detail.spec.ts` checks the run page's live log
against them; `e2e/monitor.spec.ts` checks the Monitor's list, its grid at each tile count and the tiles' live
traces the same way, and `e2e/layout-width.spec.ts` measures every area's content frame at 1920 by 1080.
`e2e/run-outputs.spec.ts` sends a run's files the same way, through the hub's routes and the stack's moto bucket, with
`e2e/fixtures/clip.webm` (three seconds of ffmpeg's testsrc2 in VP8) as its video. `e2e/inbox.spec.ts` asks decisions and sends notices the same way, then checks the bell, the Inbox and
the answers from the Inbox and from a plan run's page against what reaches the run's inbox. For the terminal the stack plays the worker's end (`FakeTerminal` in `e2e/hub_stack.py`, driven through
`e2e/support/terminal.ts`): once a browser waits on a run it connects with the worker's token over a real PTY, whose line
discipline echoes what is typed and whose program answers each line with `echo: <line>` and each resize with
`size: <cols>x<rows>`. The browser opens the websocket on the web's own origin and the web forwards it to the API, as
the reverse proxy does in production.

Specs tagged `@deployed` also run against a deployed hub. Save a signed-in session once with
`pnpm exec playwright codegen --save-storage=e2e/.auth/hub.json https://hub.example.org` (sign in by
hand, then close the window), then:

```sh
PLAYWRIGHT_BASE_URL=https://hub.example.org EVO_E2E_STORAGE_STATE=e2e/.auth/hub.json pnpm exec playwright test
```

Only the `@deployed` specs run in that mode, and they change nothing on the hub.

## Screenshots

`pnpm screenshots` writes the images the repo's README and the product page show, from a demo that is made up from
start to end: Larkspur, an online plant shop with three repos (`larkspur-api`, `larkspur-web`, `larkspur-worker`), three
people and their machines, a plan whose steps branch three ways, three runs of it in progress, a plan run waiting for
an answer in the Inbox, a run whose test log went to the spool, and the knowledge graph of the three repos.

```sh
EVO_HUB_TEST_DSN=postgresql://postgres:test@localhost:55433/postgres pnpm screenshots
```

It runs `e2e/demo/playwright.config.ts`, which starts a stack of its own as the end-to-end tests do, on ports 3344,
18344 and 18345 so it can run beside them. `e2e/demo/screenshots.shots.ts` seeds the demo through the real API
(`e2e/demo/seed.ts`, from the people, plans and events of `story.ts` and `trace.ts`), and three routes of the stack
(`e2e/demo/stack.py`) do what the API cannot: write the demo's git repos and harness and build their graph, set the
times the hub stamped to the demo's fixed clock (2026-10-20 14:30 UTC), and keep its workers online. Every page is
taken in light and dark, at 1440 and 375 px, in English and UTC with reduced motion, as
`docs/images/<page>-<width>-<scheme>.png`; a rerun writes the same file names. `scripts/screenshots.mjs` then
compresses each image to a palette PNG with sharp. Arguments go on to `playwright test`, such as `--headed`.

Keep the demo made up: names, logins, hosts and repos in `e2e/demo/` are invented, and nothing in it may name a real
project, customer or person.
