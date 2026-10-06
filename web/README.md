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

Sign-in is GitHub's web flow on the API (`/v1/auth/web/login`), so the API needs
`EVO_HUB_PUBLIC_URL` set to the address the browser uses for the web, plus the client secret and session
secret. The session cookie is the API's, httpOnly; server components forward it to the API and keep nothing.

## Scripts

| Command | What it does |
| --- | --- |
| `pnpm lint` | ESLint, no warnings allowed |
| `pnpm typecheck` | route types, then `tsc --noEmit` |
| `pnpm test` | Vitest unit tests |
| `pnpm gen:api` | regenerates `src/lib/api/schema.d.ts` from `evo-agents hub openapi` (this checkout's Python; set `PYTHON` to choose the interpreter) |
| `pnpm build`, `pnpm start` | production build, then the standalone server |
| `pnpm exec playwright test` | end-to-end tests, see below |

CI fails when `schema.d.ts` differs from what `pnpm gen:api` writes. After changing an API model, run it and
commit the result.

## End-to-end tests

Playwright starts two servers: `e2e/hub_stack.py` (a database of its own on the test Postgres, a fake GitHub,
moto's S3 as the blob store, `evo-agents hub serve` from this checkout) and the web's production build. Tests
sign in through the real web flow against the fake GitHub and seed projects, grants, memories and skills through
the API; a skill bundle is downloaded from the moto bucket the way a browser follows a presigned URL, and the
knowledge graph specs push a small synthetic graph and have the stack build it (`e2e/kg_seed.py`).

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

`E2E_SCREENSHOT_DIR=<dir>` turns on `e2e/screenshots.spec.ts`, `e2e/memories-skills-screenshots.spec.ts`,
`e2e/kg-screenshots.spec.ts` and `e2e/runs-screenshots.spec.ts`, which write review screenshots of the shell, the
memories, skills, knowledge graph and runs pages (a run's page and its diff, the Run plan dialog, a plan run's banner
and page included) in light and dark (the memories, skills and runs ones also at 375 px).

The runs specs are the worker themselves: they claim runs, report states and send events, messages and diffs with a
worker token, as the daemon does (`e2e/support/runs.ts`), and `e2e/run-detail.spec.ts` checks the run page's live log
against them. For the terminal the stack plays the worker's end (`FakeTerminal` in `e2e/hub_stack.py`, driven through
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
