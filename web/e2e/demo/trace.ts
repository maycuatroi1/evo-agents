import { createHash } from "node:crypto";

import type { WorkerEvent } from "../support/runs";

import { ago } from "./story";

/**
 * What the agents of the demo's runs did, as their workers send it (ACP events with the times they happened). Tomás's
 * run is the one the README shows: its test run printed more than the spool hook keeps in the agent's context, so the
 * agent got the start and the end around the hook's note, and the whole log stayed in a file on Tomás's machine.
 */

/** A tool call id as Claude Code makes them, the same for the same `seed` on every run of the demo. */
function toolId(seed: string): string {
  const digest = createHash("sha256").update(seed).digest("base64").replace(/[+/=]/g, "");
  return `toolu_01${digest.slice(0, 22)}`;
}

function message(minutes: number, text: string): WorkerEvent {
  return { kind: "agent_message_chunk", at: ago(minutes), body: { content: { type: "text", text } } };
}

function plan(minutes: number, entries: [string, "completed" | "in_progress" | "pending"][]): WorkerEvent {
  return { kind: "plan", at: ago(minutes), body: { entries: entries.map(([content, status]) => ({ content, status, priority: "medium" })) } };
}

type Tool = { title: string; kind: "execute" | "read" | "edit" | "search"; input: Record<string, string> };

const bash = (command: string, description: string): Tool => ({ title: "Bash", kind: "execute", input: { command, description } });
const read = (file: string): Tool => ({ title: "Read", kind: "read", input: { file_path: file } });
const edit = (file: string, before: string, after: string): Tool => ({
  title: "Edit",
  kind: "edit",
  input: { file_path: file, old_string: before, new_string: after },
});
const grep = (pattern: string, path: string): Tool => ({ title: "Grep", kind: "search", input: { pattern, path } });

/** A call started `minutes` ago; with `output`, it ended `took` minutes later with that output; else it still runs. */
function call(seed: string, minutes: number, tool: Tool, output?: string, took = 0.05): WorkerEvent[] {
  const id = toolId(seed);
  const started: WorkerEvent = {
    kind: "tool_call",
    at: ago(minutes),
    body: { toolCallId: id, title: tool.title, kind: tool.kind, status: "in_progress", rawInput: tool.input },
  };
  if (output === undefined) return [started];
  return [
    started,
    {
      kind: "tool_call_update",
      at: ago(minutes - took),
      body: { toolCallId: id, status: "completed", content: [{ type: "content", content: { type: "text", text: output } }] },
    },
  ];
}

// Tomás's run: step 2 of Checkout v2, the saved carts endpoints in larkspur-api.

export const TOMAS_SESSION = "b4c1e7d2-5a3f-4e6b-9c8d-2f1a0e3b7c64";
/** The call whose output the spool hook cut: the README's image opens it. */
export const SPOOLED_CALL = toolId("tomas-pytest-all");
const SPOOL_FILE = `/Users/tomas/.evo/tool-output/${TOMAS_SESSION}/${SPOOLED_CALL}.txt`;

const TEST_FILES: [string, string[], number][] = [
  ["tests/test_carts.py", ["test_load_cart", "test_add_item", "test_remove_item", "test_quantity_limits"], 6],
  ["tests/test_saved_carts.py", ["test_save_cart", "test_list_saved_carts", "test_restore_saved_cart", "test_saved_cart_expires"], 3],
  ["tests/test_checkout.py", ["test_place_order", "test_declined_card_queues_retry", "test_order_totals", "test_tax_by_region"], 8],
  ["tests/test_orders.py", ["test_order_status", "test_cancel_order", "test_refund", "test_order_history"], 7],
  ["tests/test_products.py", ["test_product_page", "test_out_of_stock", "test_price_rounding", "test_care_guide"], 9],
  ["tests/test_search.py", ["test_search_ranks_names_first", "test_search_typos", "test_search_filters"], 8],
  ["tests/test_shipping.py", ["test_rates", "test_free_shipping_threshold", "test_plant_safe_packaging"], 7],
  ["tests/test_migrations.py", ["test_upgrade", "test_downgrade"], 4],
];

/** `uv run pytest -v` on larkspur-api: 318 tests, two warnings. */
function pytestLog(): string {
  const ids: string[] = [];
  for (const [file, names, cases] of TEST_FILES) {
    for (const name of names) for (let index = 0; index < cases; index += 1) ids.push(`${file}::${name}[case-${index + 1}]`);
  }
  while (ids.length < 318) ids.push(`tests/test_health.py::test_health[probe-${ids.length}]`);
  const lines = ids.slice(0, 318).map((id, index) => `${`${id} PASSED`.padEnd(72)} [${String(Math.floor((100 * (index + 1)) / 318)).padStart(3)}%]`);
  return [
    "============================= test session starts ==============================",
    "platform darwin -- Python 3.12.7, pytest-8.3.3, pluggy-1.5.0 -- /Users/tomas/github/larkspur-api/.venv/bin/python",
    "cachedir: .pytest_cache",
    "rootdir: /Users/tomas/github/larkspur-api",
    "configfile: pyproject.toml",
    "plugins: anyio-4.6.2, cov-5.0.0",
    "collecting ... collected 318 items",
    "",
    ...lines,
    "",
    "=============================== warnings summary ===============================",
    "tests/test_checkout.py::test_declined_card_queues_retry[case-3]",
    "  /Users/tomas/github/larkspur-api/app/queue.py:12: DeprecationWarning: datetime.utcnow() is deprecated",
    "",
    "tests/test_shipping.py::test_rates[case-5]",
    "  /Users/tomas/github/larkspur-api/app/shipping.py:48: UserWarning: carrier rate table is 9 days old",
    "",
    "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html",
    "======================= 318 passed, 2 warnings in 41.27s =======================",
  ].join("\n");
}

/** What the agent got of the log: its start and end around the hook's note (evo_agents.hub.spool.excerpt). */
function spooled(text: string): string {
  const note =
    `[evo-hub: this output has ${text.length.toLocaleString("en-US")} characters and only its start and end are shown here; all of it ` +
    `is in ${SPOOL_FILE} (kept 7 days). Read it there, with offset and limit or grep, rather than running the call again.]`;
  let start = text.slice(0, 1500);
  let end = text.slice(-1500);
  const head = start.lastIndexOf("\n");
  if (head >= 750) start = start.slice(0, head);
  const tail = end.indexOf("\n");
  if (tail >= 0 && tail < 750) end = end.slice(tail + 1);
  return `${start}\n\n${note}\n\n${end}`;
}

const CARTS_BEFORE = `@router.get("/current")
def current(customer_id: int):
    return load_cart(customer_id)
`;
const CARTS_AFTER = `@router.get("/current")
def current(customer_id: int):
    return load_cart(customer_id)


@router.get("/saved")
def saved(customer_id: int):
    """The shopper's saved carts that have not expired, newest first (CHK-01)."""
    return session().execute(
        "SELECT * FROM saved_carts WHERE customer_id = %s AND expires_at > now() ORDER BY saved_at DESC",
        (customer_id,),
    ).all()


@router.post("/{cart_id}/save")
def save(cart_id: int, customer_id: int):
    return session().execute(
        "INSERT INTO saved_carts (customer_id) VALUES (%s) RETURNING id", (customer_id,)
    ).one()
`;

export function tomasTrace(): WorkerEvent[] {
  return [
    message(24.7, "Step 2 of Checkout v2: the saved carts endpoints in `app/carts.py`. I'll read the cart code and migration 0042 first."),
    plan(24.6, [
      ["Read app/carts.py and migration 0042", "in_progress"],
      ["Add GET /carts/saved and POST /carts/{id}/save", "pending"],
      ["Tests for both endpoints", "pending"],
      ["Run the suite, then ruff and mypy", "pending"],
    ]),
    ...call("tomas-read-carts", 24.4, read("app/carts.py"), '"""Carts: the one a shopper is filling, and the carts they saved for later (CHK-01)."""\n\nfrom fastapi import APIRouter\n...'),
    ...call("tomas-read-migration", 24.2, read("migrations/0042_saved_carts.sql"), "-- CHK-01: carts a shopper keeps for later, for 30 days\nCREATE TABLE saved_carts (\n..."),
    ...call("tomas-grep", 23.9, grep("load_cart", "app"), "app/carts.py:12:def load_cart(customer_id: int):\napp/checkout.py:5:from app.carts import load_cart"),
    message(23.2, "Both endpoints fit next to `load_cart`. Saved carts expire after 30 days, so the list filters on `expires_at`."),
    ...call("tomas-edit-carts", 22.5, edit("app/carts.py", CARTS_BEFORE, CARTS_AFTER), "The file app/carts.py has been updated."),
    ...call(
      "tomas-edit-tests",
      19.8,
      edit("tests/test_saved_carts.py", "", "def test_save_cart(client, customer):\n    saved = client.post(f\"/carts/{customer.cart_id}/save\", params={\"customer_id\": customer.id})\n    assert saved.status_code == 200\n"),
      "The file tests/test_saved_carts.py has been created.",
    ),
    ...call("tomas-pytest-new", 17.4, bash("uv run pytest tests/test_saved_carts.py -q", "Run the new tests"), "............                                                             [100%]\n12 passed in 1.84s", 0.1),
    plan(16.9, [
      ["Read app/carts.py and migration 0042", "completed"],
      ["Add GET /carts/saved and POST /carts/{id}/save", "completed"],
      ["Tests for both endpoints", "completed"],
      ["Run the suite, then ruff and mypy", "in_progress"],
    ]),
    ...call("tomas-pytest-all", 16.6, bash("uv run pytest -v", "Run the whole test suite"), spooled(pytestLog()), 0.75),
    message(15.5, "All 318 tests pass, the 12 new ones among them. The whole log is in the spool file; the summary at its end is all I need from it."),
    ...call("tomas-read-checkout", 14.8, read("app/checkout.py"), '"""Checkout: a cart becomes an order; a declined card is queued for retry (CHK-02)."""\n...'),
    message(14.2, "Checkout should take a saved cart too, so a shopper who restores one goes straight to payment."),
    ...call(
      "tomas-edit-checkout",
      12.6,
      edit("app/checkout.py", "def place_order(customer_id: int):\n    cart = load_cart(customer_id)", "def place_order(customer_id: int, saved_cart_id: int | None = None):\n    cart = load_saved_cart(saved_cart_id) if saved_cart_id else load_cart(customer_id)"),
      "The file app/checkout.py has been updated.",
    ),
    ...call("tomas-pytest-checkout", 9.1, bash("uv run pytest tests/test_checkout.py -q", "Run the checkout tests"), "..........................................                               [100%]\n42 passed in 3.06s", 0.1),
    ...call("tomas-lint", 2, bash("uv run ruff check . && uv run mypy app", "Lint and type-check")),
  ];
}

// Priya's run: step 3 of Checkout v2, payment retries in larkspur-worker.

export function priyaTrace(): WorkerEvent[] {
  return [
    message(17.6, "Step 3: retry a declined card three times over 24 hours before the order fails. Reading `worker/payments.py`."),
    ...call("priya-read-payments", 17.3, read("worker/payments.py"), '"""Retrying a declined card (CHK-02): three tries over 24 hours, then the order fails."""\n...'),
    ...call("priya-grep", 16.8, grep("payments.retry", "."), "worker/__main__.py:14:    \"payments.retry\": retry_payment,"),
    message(15.9, "The backoff is there but nothing schedules the next try. I'll queue it from `retry_payment` and keep the attempt count on the order."),
    ...call(
      "priya-edit-payments",
      13.2,
      edit("worker/payments.py", '    send_retry_notice(order_id, BACKOFF[attempt])\n    return "retrying"', '    send_retry_notice(order_id, BACKOFF[attempt])\n    enqueue("payments.retry", {"order_id": order_id, "attempt": attempt + 1}, delay=BACKOFF[attempt])\n    return "retrying"'),
      "The file worker/payments.py has been updated.",
    ),
    ...call("priya-pytest", 6.4, bash("uv run pytest tests/test_payments.py -q", "Run the payment tests"), "...................                                                      [100%]\n19 passed in 0.92s", 0.1),
    ...call("priya-edit-tests", 1.2, edit("tests/test_payments.py", "", "def test_third_decline_fails_the_order(fake_charge):\n    assert retry_payment(7, 3, fake_charge.declining()) == \"failed\"\n")),
  ];
}

// Maya's run: step 4 of Checkout v2, the checkout page in larkspur-web.

export function mayaTrace(): WorkerEvent[] {
  return [
    message(10.6, "Step 4: the checkout page lists saved carts above the current one. Reading the page first."),
    ...call("maya-read-page", 10.3, read("src/checkout/CheckoutPage.tsx"), 'import { useEffect, useState } from "react";\n...'),
    ...call(
      "maya-edit-page",
      8.1,
      edit("src/checkout/CheckoutPage.tsx", "  if (!cart) return <p>Loading your cart</p>;", "  if (!cart) return <p>Loading your cart</p>;\n  const saved = useSavedCarts();"),
      "The file src/checkout/CheckoutPage.tsx has been updated.",
    ),
    ...call("maya-vitest", 1.4, bash("pnpm vitest run src/checkout", "Run the checkout tests"), " ✓ src/checkout/SavedCarts.test.tsx (9 tests) 212ms\n ✓ src/checkout/CheckoutPage.test.tsx (8 tests) 340ms\n\n Test Files  2 passed (2)\n      Tests  17 passed (17)", 0.2),
  ];
}

// The plan run: Product search on Postgres full-text, on Maya's machine. Before the notice, after it, and before its ask.

export function planRunTrace(): { merged: WorkerEvent[]; checkpoint: WorkerEvent[] } {
  return {
    merged: [
      message(46.6, "Plan postgres-search, step 1: the search vector column and its GIN index in larkspur-api."),
      ...call("plan-edit-migration", 45.1, edit("migrations/0043_search_vector.sql", "", "ALTER TABLE products ADD COLUMN search_vector tsvector;\nCREATE INDEX products_search_vector ON products USING gin (search_vector);\n"), "The file migrations/0043_search_vector.sql has been created."),
      ...call("plan-pytest-search", 41.2, bash("uv run pytest tests/test_search.py", "Compare the 500 queries"), "tests/test_search.py ........................                             [100%]\n24 passed in 6.40s\ntop-3 overlap with the hosted index: 97.2% (486 of 500 queries)", 0.3),
      message(40.2, "Step 1 holds: 97% of the top-3 results match. Step 2: the backfill job in larkspur-worker."),
      ...call("plan-edit-reindex", 36.5, edit("worker/reindex.py", "", "BATCH = 500\n\n\ndef reindex(conn, after_id: int = 0) -> int:\n    ..."), "The file worker/reindex.py has been created."),
      ...call("plan-pytest-reindex", 24.4, bash("uv run pytest tests/test_reindex.py", "Run the reindex tests on a copy of staging"), "88 passed in 412.80s (0:06:52)", 6.9),
      ...call("plan-merge", 19.6, bash("gh pr merge 214 --squash --delete-branch", "Merge the backfill job"), "✓ Squashed and merged pull request larkspur-shop/larkspur-worker#214 (Backfill job for product vectors)", 0.2),
    ],
    checkpoint: [
      message(18.2, "Step 3 is the checkpoint: it reindexes staging, so I'll check what it would do and ask before running it."),
      ...call("plan-dry-run", 12.3, bash("uv run python -m scripts.compare_search --env staging --dry-run", "Dry run on staging"), "500 queries: top-3 overlap 97.2% (486 of 500)\nreindex estimate: 48,210 products, about 6 min 40 s", 0.4),
      message(6.2, "The dry run matches step 1's numbers. Reindexing staging needs your go-ahead."),
    ],
  };
}
