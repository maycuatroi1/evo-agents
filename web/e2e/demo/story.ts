import { ADMIN_GITHUB_ID, type Account, type Repo } from "../support/hub";

import { DEMO_ADMIN_LOGIN } from "./env";

/**
 * The demo the README's images show, all of it made up: Larkspur, a small online plant shop, builds its store in three
 * repos (an API, a web front and a background worker) with a harness beside them. Three people run agents on their
 * own machines. Checkout v2 is a plan whose steps branch three ways after the first and meet again at a staging
 * checkpoint; three of its steps run at once. A plan run moving product search to Postgres reached its checkpoint and
 * asks Maya whether to reindex staging now, which waits in her Inbox. Tomás's run printed a test log too long for the
 * agent's context, which the spool hook cut. The repos also feed the project's knowledge graph (demo/stack.py).
 *
 * Every time is set against NOW, a fixed instant in the past: the events say when they happened, the stack sets the
 * times the hub stamped itself (POST /demo/retime), and the browser's clock stands at NOW.
 */
export const NOW = Date.parse("2026-10-20T14:30:00Z");

/** NOW less `minutes`, as the API writes times. */
export function ago(minutes: number): string {
  return new Date(NOW - Math.round(minutes * 60_000)).toISOString();
}

export type Person = Account & { name: string };

/** The hub admin of the demo's stack, signed in on every image. */
export const MAYA: Person = { login: DEMO_ADMIN_LOGIN, id: ADMIN_GITHUB_ID, name: "Maya Lindqvist" };
export const TOMAS: Person = { login: "tomas-reyes", id: 52_318_604, name: "Tomás Reyes" };
export const PRIYA: Person = { login: "priya-raman", id: 61_045_977, name: "Priya Raman" };
/** The newcomer Maya invites: the invite page, then the Get started list once she accepted. */
export const ELENA: Person = { login: "elena-varga", id: 70_284_513, name: "Elena Varga" };
export const PEOPLE = [MAYA, TOMAS, PRIYA, ELENA];

export const PROJECT = "larkspur";
export const API = "larkspur-api";
export const WEB = "larkspur-web";
export const WORKER = "larkspur-worker";
export const HARNESS = "larkspur-harness";
const ORIGIN = "https://github.com/larkspur-shop";

export const REPOS: Repo[] = [API, WEB, WORKER].map((name) => ({ name, origin: `${ORIGIN}/${name}`, default_branch: "main", path: name }));

/** The project as `evo-agents hub project register` sends it from the harness. */
export const REGISTRATION = {
  levels: ["public", "internal", "customer", "secret"],
  locations: ["any"],
  default_label: { level: "internal", integrity: "U" },
  sinks: [
    { id: "hub", kind: "hub", clearance: { level: "internal" } },
    { id: "claude-code", kind: "agent-session", clearance: { level: "customer" } },
  ],
  repos: REPOS,
  harness: { name: HARNESS, workspace: "~/code/larkspur", path: HARNESS },
};

type StepIn = {
  id: number;
  title: string;
  repo: string;
  what: string;
  verify: string;
  status: "pending" | "in_progress" | "done";
  depends_on?: number[];
  evidence?: string;
  done_at?: string;
};

/** Checkout v2: step 1, then three branches (2, 3 then 5, 4) that meet at the staging checkpoint, then the release. */
export const CHECKOUT_PLAN = "checkout-v2";
const CHECKOUT_STEPS: StepIn[] = [
  {
    id: 1,
    title: "Cart tables and migration",
    repo: API,
    what: "Add saved_carts and saved_cart_items with migration 0042, keyed by customer, with a 30-day expiry.",
    verify: "uv run pytest tests/test_migrations.py",
    status: "done",
    evidence: "larkspur-api@8d41c2e: migration 0042 applied to a copy of production in 2.1 s; 306 tests pass.",
    done_at: ago(26 * 60),
  },
  {
    id: 2,
    title: "Saved carts endpoint",
    repo: API,
    what: "GET /carts/saved lists the shopper's saved carts and POST /carts/{id}/save keeps one; both in app/carts.py.",
    verify: "uv run pytest -q",
    status: "pending",
    depends_on: [1],
  },
  {
    id: 3,
    title: "Payment retries in the worker",
    repo: WORKER,
    what: "Retry a declined card three times over 24 hours with backoff before the order fails, in worker/payments.py.",
    verify: "uv run pytest tests/test_payments.py",
    status: "pending",
    depends_on: [1],
  },
  {
    id: 4,
    title: "Checkout page with saved carts",
    repo: WEB,
    what: "The checkout page lists saved carts above the current one and restores one in a click.",
    verify: "pnpm vitest run src/checkout",
    status: "pending",
    depends_on: [1],
  },
  {
    id: 5,
    title: "Retry notice email",
    repo: WORKER,
    what: "Tell the shopper when a payment will be retried, and when it finally fails, in worker/emails.py.",
    verify: "uv run pytest tests/test_emails.py",
    status: "pending",
    depends_on: [3],
  },
  {
    id: 6,
    title: "Checkpoint: staging rollout",
    repo: API,
    what: "Deploy the three repos to staging, place ten test orders with the declined-card fixture, and ask before production.",
    verify: "uv run python -m scripts.smoke --env staging",
    status: "pending",
    depends_on: [2, 4, 5],
  },
  {
    id: 7,
    title: "Release notes and feature flag",
    repo: WEB,
    what: "Turn on checkout_v2 for 10% of shoppers and write the release notes.",
    verify: "pnpm test",
    status: "pending",
    depends_on: [6],
  },
];

export const CHECKOUT_BODY = {
  id: CHECKOUT_PLAN,
  title: "Checkout v2: saved carts and payment retries",
  goal: "Shoppers keep a cart across devices, and a declined card is retried before the order fails.",
  repos: [
    { repo: API, branch: "feat/checkout-v2", status: "in_progress" },
    { repo: WORKER, branch: "feat/checkout-v2", status: "in_progress" },
    { repo: WEB, branch: "feat/checkout-v2", status: "in_progress" },
  ],
  steps: CHECKOUT_STEPS,
};

/** The plan the plan run works through, on one machine: steps 1 and 2 done, step 3 the checkpoint it asks about. */
export const SEARCH_PLAN = "postgres-search";
export const SEARCH_BRANCH = "feat/postgres-search";
export const SEARCH_BODY = {
  id: SEARCH_PLAN,
  title: "Product search on Postgres full-text",
  goal: "Search runs on Postgres full-text instead of the hosted index, with the same top results for the 500 most common queries.",
  repos: [
    { repo: API, branch: SEARCH_BRANCH, status: "in_progress" },
    { repo: WORKER, branch: SEARCH_BRANCH, status: "in_progress" },
    { repo: WEB, branch: SEARCH_BRANCH, status: "pending" },
  ],
  steps: [
    {
      id: 1,
      title: "Search vector column and GIN index",
      repo: API,
      what: "Add products.search_vector, weighted name over description, with a GIN index (migration 0043).",
      verify: "uv run pytest tests/test_search.py",
      status: "done",
      evidence: "larkspur-api@3e5d9a1: the 500-query fixture keeps 97% of its top-3 results; 312 tests pass.",
      done_at: ago(40),
    },
    {
      id: 2,
      title: "Backfill job for product vectors",
      repo: WORKER,
      what: "worker/reindex.py fills search_vector in batches of 500 and can resume after a stop.",
      verify: "uv run pytest tests/test_reindex.py",
      status: "done",
      depends_on: [1],
      evidence: "larkspur-worker@b72f0e4: a copy of staging reindexed in 6 min 40 s; 88 tests pass.",
      done_at: ago(19),
    },
    {
      id: 3,
      title: "Checkpoint: reindex staging",
      repo: API,
      what: "Run the backfill on staging, compare the 500 queries against the hosted index, and stop for the owner first.",
      verify: "uv run python -m scripts.compare_search --env staging",
      status: "pending",
      depends_on: [2],
    },
    {
      id: 4,
      title: "Search box on the new endpoint",
      repo: WEB,
      what: "The search box calls /search?q= on the API and drops the hosted index's client.",
      verify: "pnpm vitest run src/search",
      status: "pending",
      depends_on: [3],
    },
    {
      id: 5,
      title: "Retire the hosted index sync",
      repo: WORKER,
      what: "Remove the nightly job that pushes products to the hosted index, and its API key from the worker's settings.",
      verify: "uv run pytest tests/test_jobs.py",
      status: "pending",
      depends_on: [4],
    },
  ],
};

/** The machines: each person's own, reporting checkouts of the repos it works in. */
export const MACHINES = {
  maya: { owner: MAYA, name: "maya-studio", slots: 2, repos: [API, WEB, WORKER] },
  tomas: { owner: TOMAS, name: "tomas-macbook", slots: 1, repos: [API] },
  priya: { owner: PRIYA, name: "priya-mini", slots: 1, repos: [WORKER] },
};

/** The question the plan run asks at its checkpoint, as `evo-agents worker ask` sends it. */
export const DECISION = {
  category: "deploy",
  step_key: "3",
  question: "Reindex the 48,210 products on staging now?",
  context:
    "Step 3 rebuilds the search vectors of every product on **staging**. On a copy of staging it took 6 min 40 s, and " +
    "search kept answering from the old index until the swap. No staging deploy is queued today.",
  options: [
    { key: "now", label: "Reindex staging now", description: "About 7 minutes; search stays up." },
    { key: "tonight", label: "Wait for tonight's window", description: "The run parks until 22:00 UTC." },
  ],
  recommended: "now",
};
export const DECISION_AT = ago(6);

/** What the plan run told Maya when step 2 merged. */
export const NOTICE = {
  kind: "merge_default_branch" as const,
  title: "Merged the backfill job into larkspur-worker main",
  body: "Step 2 is done: worker/reindex.py fills product vectors in batches of 500 and resumes after a stop.",
  repo: WORKER,
  branch: "main",
  commits: ["b72f0e4a1c9d3e5f7a8b0c2d4e6f8a1b3c5d7e9f", "5c0a9d8e7f6b5a4c3d2e1f0a9b8c7d6e5f4a3b2c"],
};
export const NOTICE_AT = ago(19);
