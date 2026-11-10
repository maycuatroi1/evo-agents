import path from "node:path";

import type { Locator, Page, Route } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";

import { createAgentByApi } from "./support/agents";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { chainSpec, putSystemByApi, systemPath } from "./support/systems";

/**
 * Observe mode of an agent system and the page of one trace (web/DESIGN.md, Observe: work and timeline). The system is
 * real; its traces, metrics, 24-hour numbers and trace stream are answered by the page's routes, since no worker runs
 * agents in this stack. Observe opens on the numbers of the last 24 hours and the work list with its filters; selecting
 * a work shows its timeline by agent (bars, a mark where a person was asked, the axis and the key) and its hand-offs
 * with those off the design counted, and the map draws only that work's path. The canvas shows each node's state and
 * numbers, counts on the edges work crossed and none on the others, an off-design crossing as a dashed edge to a node
 * outside the design; a work that moves arrives through the stream without a reload, a hidden tab stops the stream,
 * and a trace's page lays out its spans by agent. Nothing says the stream is live while all is well. axe checks the
 * page in light and dark. With E2E_SCREENSHOT_DIR set it writes review screenshots. Pages render in English.
 */
test.skip(isDeployed, "creates agents and systems through the local stack");

type Span = Record<string, unknown> & { id: number; started_at: string };

function main(page: Page) {
  return page.locator("#main");
}

/** Observe's canvas: the designer, opened first, stays mounted and hidden under it with nodes and edges of its own. */
function canvas(page: Page): Locator {
  return main(page).getByTestId("system-observe-canvas");
}

function node(page: Page, id: string): Locator {
  return canvas(page).locator(`[data-testid="system-node"][data-node="${id}"]`);
}

function edge(page: Page, id: string): Locator {
  return canvas(page).locator(`[data-testid="system-edge"][data-edge="${id}"]`);
}

function facet(page: Page, value: string): Locator {
  return main(page).getByTestId("system-work-filters").locator(`[data-facet-value="${value}"]`);
}

const linkOf = (id: number) => `[data-testid="system-work-link"][data-trace="${id}"]`;

function workLink(page: Page, id: number): Locator {
  return main(page).locator(linkOf(id));
}

/** The row of work `id` in the table. */
function workRow(page: Page, id: number): Locator {
  return main(page).getByTestId("system-work-table").locator("tr").filter({ has: page.locator(linkOf(id)) });
}

/** axe on what the page shows, in the light theme and then in the dark one. */
async function bothThemes(page: Page, label: string) {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
    await expectNoSeriousViolations(page, `${label} (${scheme})`);
  }
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
}

/** Review screenshots of Observe with a work selected: light and dark at desktop width, then 375 px. */
async function screenshots(page: Page, dir: string) {
  // A full-page shot of a scrolled page draws the sticky header midway: start from the top.
  await page.evaluate(() => window.scrollTo(0, 0));
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
    await page.screenshot({ path: path.join(dir, `observe-work-timeline-${scheme}.png`), fullPage: true });
  }
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(main(page).getByTestId("system-timeline")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(dir, "observe-work-timeline-375.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
}

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function span(id: number, patch: Partial<Span>): Span {
  return {
    id,
    parent_id: null,
    name: "invoke_agent",
    tool: null,
    agent: "",
    node: null,
    from_node: null,
    to_node: null,
    run_id: null,
    project: null,
    runtime: "claude",
    status: "ok",
    off_design: false,
    reason: null,
    message_id: null,
    attributes: {},
    started_at: ago(120_000),
    ended_at: ago(100_000),
    seconds: 20,
    input_tokens: 0,
    output_tokens: 0,
    usd: 0,
    model: null,
    run_state: "done",
    content_withheld: false,
    ...patch,
  };
}

type Work = Partial<{ title: string; goal: string; done_when: string; status: string; agent: string | null; waiting: Record<string, unknown> | null; work_chat_id: number }>;

function summary(id: number, slug: string, spans: Span[], work: Work = {}) {
  const runs = spans.filter((one) => one.name === "invoke_agent");
  return {
    id,
    system: slug,
    revision: 1,
    chat_id: 7,
    message_id: 70 + id,
    opened_by: "rin",
    started_at: spans[0].started_at,
    last_at: ago(1_000),
    runs: runs.length,
    off_design: spans.filter((one) => one.off_design).length,
    errors: spans.filter((one) => one.status === "error" || one.status === "refused").length,
    usd: runs.reduce((sum, one) => sum + Number(one.usd), 0),
    work_chat_id: null,
    title: null,
    goal: null,
    done_when: null,
    status: null,
    agent: null,
    waiting: null,
    ...work,
  };
}

test("Observe lists the work with its filters, shows a work's timeline, hand-offs and path, and a trace's waterfall", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const triage = uniqueName("lumen-triage");
  const billing = uniqueName("lumen-billing");
  const ledger = uniqueName("lumen-ledger");
  const legal = `@${uniqueName("lumen-legal")}`;
  await createAgentByApi(me, project, { handle: triage, name: "Triage", permissions: {} });
  await createAgentByApi(me, project, { handle: billing, name: "Billing", permissions: {} });
  await createAgentByApi(me, project, { handle: ledger, name: "Ledger", permissions: {} });
  const slug = uniqueName("desk");
  const chain = [triage, billing, ledger].map((handle) => ({ handle, version: 1 }));
  await putSystemByApi(me, project, slug, chainSpec("Northwind support desk", chain));

  // Work 40 is done: triage answered alone. Work 41 failed: triage asked billing, billing queried the docs graph,
  // parked on a decision put to the member, asked an agent outside the design (refused) and failed. Work 42 opens later,
  // through the stream: triage works on a new message and waits on the member's decision. Nothing reaches the ledger.
  const done = [span(1, { agent: triage, node: triage, from_node: "entry", run_id: 499, project, started_at: ago(3_600_000), ended_at: ago(3_500_000), usd: 0.01 })];
  const older = [
    span(11, { agent: triage, node: triage, from_node: "entry", run_id: 501, project, model: "sonnet", input_tokens: 1200, output_tokens: 300, usd: 0.0123, message_id: 71 }),
    span(12, { parent_id: 11, name: "execute_tool", tool: "agent_ask", agent: triage, node: triage, to_node: billing, run_state: null, started_at: ago(110_000), ended_at: ago(110_000), seconds: 0, message_id: 72 }),
    span(13, { parent_id: 12, agent: billing, node: billing, from_node: triage, run_id: 502, project, status: "error", run_state: "failed", started_at: ago(109_000), ended_at: ago(90_000), usd: 0.004 }),
    span(14, { parent_id: 13, name: "execute_tool", tool: "kg_search", agent: billing, node: billing, run_state: null, attributes: { "gen_ai.data_source.id": "docs", "evo.kg.results": 3 }, started_at: ago(105_000), ended_at: ago(104_000), seconds: 1 }),
    span(15, { parent_id: 13, name: "execute_tool", tool: "agent_ask", agent: billing, node: billing, to_node: legal, status: "refused", off_design: true, reason: "no edge to this agent", run_state: null, started_at: ago(100_000), ended_at: ago(100_000), seconds: 0 }),
  ];
  const newer = [span(21, { agent: triage, node: triage, from_node: "entry", run_id: 503, project, status: "running", run_state: "running", started_at: ago(2_000), ended_at: null, seconds: null })];
  const work: Record<number, Work> = {
    40: { title: "Reset the lumenops.example password", status: "done", agent: null, work_chat_id: 90 },
    41: { title: "Refund order 1182", goal: "Refund the duplicate charge", done_when: "The customer has the refund", status: "failed", agent: billing, work_chat_id: 91 },
    42: { title: "Refund order 1190", status: "needs_person", agent: triage, waiting: { kind: "decision", decision_id: 8, login: me.login, question: "Refund over the limit?" }, work_chat_id: 92 },
  };
  const timelines: Record<number, unknown> = {
    41: {
      runs: [
        { span_id: 11, run_id: 501, agent: triage, node: triage, state: "done", segments: [{ kind: "queued", started_at: ago(121_000), ended_at: ago(120_000) }, { kind: "running", started_at: ago(120_000), ended_at: ago(100_000) }] },
        {
          span_id: 13,
          run_id: 502,
          agent: billing,
          node: billing,
          state: "failed",
          segments: [
            { kind: "running", started_at: ago(109_000), ended_at: ago(99_000) },
            { kind: "parked", started_at: ago(99_000), ended_at: ago(95_000) },
            { kind: "running", started_at: ago(95_000), ended_at: ago(90_000) },
          ],
        },
      ],
      marks: [{ decision_id: 5, run_id: 502, agent: billing, login: me.login, question: "Refund twice?", state: "answered", asked_at: ago(99_000), answered_at: ago(95_000) }],
    },
  };
  const metrics = {
    nodes: [
      { node: triage, count: 4, errors: 0, off_design: 0, p50_seconds: 20, input_tokens: 4000, output_tokens: 900, usd: 0.05 },
      { node: billing, count: 3, errors: 1, off_design: 0, p50_seconds: 19, input_tokens: 2000, output_tokens: 400, usd: 0.02 },
    ],
    edges: [
      { from_node: "entry", to_node: triage, count: 4, errors: 0, off_design: 0 },
      { from_node: triage, to_node: billing, count: 3, errors: 1, off_design: 0 },
      { from_node: billing, to_node: legal, count: 1, errors: 1, off_design: 1 },
    ],
  };
  const oneWork = {
    nodes: [
      { node: triage, count: 1, errors: 0, off_design: 0, p50_seconds: 20, input_tokens: 1200, output_tokens: 300, usd: 0.0123 },
      { node: billing, count: 1, errors: 1, off_design: 0, p50_seconds: 19, input_tokens: 0, output_tokens: 0, usd: 0.004 },
    ],
    edges: [
      { from_node: "entry", to_node: triage, count: 1, errors: 0, off_design: 0 },
      { from_node: triage, to_node: billing, count: 1, errors: 1, off_design: 0 },
      { from_node: billing, to_node: legal, count: 1, errors: 1, off_design: 1 },
    ],
  };
  const traces = new Map<number, Span[]>([
    [40, done],
    [41, older],
  ]);

  // The stream: empty until the test releases work 42, which goes out once; each empty answer makes it reconnect.
  let release = false;
  let sent = false;
  let streams = 0;
  const base = `/v1/projects/${project}/systems/${slug}`;
  await page.route(`**${base}/**`, async (route: Route) => {
    const where = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (where === `${base}/traces/stream`) {
      streams += 1;
      let body = "retry: 300\n\n";
      if (release && !sent) {
        sent = true;
        traces.set(42, newer);
        body += `event: trace\ndata: ${JSON.stringify(summary(42, slug, newer, work[42]))}\n\n`;
      }
      return route.fulfill({ status: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache" }, body });
    }
    if (where === `${base}/traces`) return json([...traces.entries()].reverse().map(([id, spans]) => summary(id, slug, spans, work[id])));
    if (where === `${base}/metrics`) return json(metrics);
    if (where === `${base}/work/summary`) {
      const waiting = traces.has(42) ? 1 : 0;
      return json({ hours: 24, since: ago(86_400_000), traces: traces.size, withheld: 0, open: waiting, needs_person: waiting, failed: 1, done: 1, closed_p50_seconds: 754, usd: 0.0263 });
    }
    const one = /\/traces\/(\d+)$/.exec(where);
    const id = Number(one?.[1]);
    const spans = one ? traces.get(id) : undefined;
    if (spans) return json({ ...summary(id, slug, spans, work[id]), spans, metrics: id === 41 ? oneWork : metrics, timeline: timelines[id] ?? { runs: [], marks: [] } });
    return route.continue();
  });

  await open(page, systemPath(project, slug));
  await main(page).getByTestId("system-mode-observe").click();
  await page.waitForURL(`**${systemPath(project, slug)}?mode=observe`);
  const observe = main(page).getByTestId("system-observe");
  await expect(observe).toHaveAttribute("data-revision", "1");
  await expect(main(page).getByTestId("system-mode-observe")).toHaveAttribute("aria-current", "page");

  // The numbers of the last 24 hours.
  const numbers = main(page).getByTestId("system-work-summary");
  await expect(numbers.getByTestId("summary-work-open-value")).toHaveText("0");
  await expect(numbers.getByTestId("summary-work-needs-person-value")).toHaveText("0");
  await expect(numbers.getByTestId("summary-work-failed-value")).toHaveText("1");
  await expect(numbers.getByTestId("summary-work-close-time-value")).toHaveText("12m 34s");
  await expect(numbers.getByTestId("summary-work-cost-value")).toHaveText("$0.0263");

  // The work list, the newest first, with what each waits for and the links to its chat and trace.
  const links = main(page).getByTestId("system-work-link");
  await expect(links).toHaveCount(2);
  expect(await links.evaluateAll((rows) => rows.map((row) => row.getAttribute("data-trace")))).toEqual(["41", "40"]);
  await expect(workLink(page, 41)).toHaveText("Refund order 1182");
  const row41 = workRow(page, 41);
  await expect(row41.getByTestId("system-work-status")).toHaveAttribute("data-status", "failed");
  await expect(row41).toContainText("Billing");
  await expect(row41.getByTestId("system-work-chat")).toHaveAttribute("href", /\/chats\/91/);
  await expect(row41.getByTestId("system-work-trace")).toHaveAttribute("href", `${systemPath(project, slug)}/traces/41`);
  await expect(main(page).getByTestId("system-work-count")).toHaveText("2 of 2");

  // The filters and their counts.
  await expect(facet(page, "")).toContainText("2");
  await expect(facet(page, "failed")).toContainText("1");
  await expect(facet(page, "needs_you")).toContainText("0");
  await facet(page, "done").click();
  await expect(page).toHaveURL(/filter=done/);
  await expect(links).toHaveCount(1);
  await expect(links.first()).toHaveAttribute("data-trace", "40");
  await facet(page, "running").click();
  await expect(main(page).getByTestId("system-work-no-match")).toBeVisible();
  await facet(page, "").click();
  await expect(links).toHaveCount(2);

  // Each node's state and numbers over all the work; a line no work crossed has no number.
  await expect(node(page, billing).getByTestId("system-node-state")).toHaveAttribute("data-state", "failed");
  await expect(node(page, triage).getByTestId("system-node-state")).toHaveAttribute("data-state", "idle");
  await expect(node(page, triage).getByTestId("system-node-numbers")).toContainText("4 runs");
  await expect(node(page, billing).getByTestId("system-node-numbers")).toContainText("3 runs");
  await expect(node(page, "entry").getByTestId("system-node-numbers")).toHaveText("2 traces");
  await expect(edge(page, "e0")).toHaveAttribute("data-count", "4");
  await expect(edge(page, "e1")).toHaveAttribute("data-count", "3");
  await expect(edge(page, "e1")).toHaveAttribute("data-off-design", "false");
  await expect(edge(page, "e0")).toHaveAttribute("data-lit", "false");
  await expect(edge(page, "e2").getByTestId("system-edge-count")).toHaveCount(0);
  const off = edge(page, `x-${billing}-${legal}`);
  await expect(off).toHaveAttribute("data-off-design", "true");
  await expect(off).toContainText("Off design");
  await expect(node(page, legal).getByTestId("system-node-numbers")).toHaveText("Not in the design");
  await expect(canvas(page).locator('[data-testid="system-node"][data-dimmed="true"]')).toHaveCount(0);

  // The node's numbers in full, under its display name.
  await node(page, billing).click();
  const detail = main(page).getByTestId("system-node-detail");
  await expect(detail).toHaveAttribute("data-node", billing);
  await expect(detail.getByRole("heading")).toHaveText("Billing");
  await expect(detail).toContainText("Failed");
  await expect(detail).toContainText("2,400");
  await detail.getByRole("button", { name: "Close the numbers" }).click();
  await expect(detail).toHaveCount(0);

  // Selecting a work: its timeline by agent, its hand-offs, and only its path on the map.
  await workLink(page, 41).click();
  await expect(page).toHaveURL(/work=41/);
  await expect(workLink(page, 41)).toHaveAttribute("aria-current", "true");
  const chosen = main(page).getByTestId("system-work-detail");
  await expect(chosen).toHaveAttribute("data-trace", "41");
  await expect(chosen.getByRole("heading", { level: 2 })).toContainText("Refund order 1182");
  await expect(chosen).toContainText("Refund the duplicate charge");
  const timeline = chosen.getByTestId("system-timeline");
  const lanes = timeline.getByTestId("system-timeline-lane");
  await expect(lanes).toHaveCount(2);
  await expect(lanes.nth(0)).toHaveAttribute("data-agent", triage);
  await expect(lanes.nth(0)).toContainText("Triage");
  await expect(lanes.nth(1)).toContainText("Billing");
  const kinds = (lane: Locator) => lane.getByTestId("system-timeline-bar").evaluateAll((bars) => bars.map((bar) => bar.getAttribute("data-kind")));
  expect(await kinds(lanes.nth(0))).toEqual(["queued", "done"]);
  expect(await kinds(lanes.nth(1))).toEqual(["failed", "parked", "failed"]);
  await expect(lanes.nth(1).getByTestId("system-timeline-mark")).toHaveAttribute("data-decision", "5");
  await expect(timeline.getByTestId("system-timeline-axis")).toContainText("0s");
  const legend = timeline.getByTestId("system-timeline-legend");
  for (const word of ["Queued", "Running", "Done", "Failed", "Parked", "Needs a person"]) await expect(legend).toContainText(word);
  const handoffs = chosen.getByTestId("system-handoffs");
  await expect(handoffs).toHaveAttribute("data-off-design", "1");
  await expect(handoffs.getByTestId("system-handoffs-off")).toHaveText("1 off design");
  const hops = handoffs.getByTestId("system-handoff");
  await expect(hops).toHaveCount(3);
  await expect(hops.nth(0)).toHaveAttribute("data-from", "entry");
  await expect(hops.nth(1)).toHaveAttribute("data-to", billing);
  await expect(hops.nth(1)).toContainText("Triage to Billing");
  await expect(hops.nth(2)).toHaveAttribute("data-refused", "true");
  await expect(hops.nth(2)).toHaveAttribute("data-off-design", "true");
  await expect(hops.nth(2)).toContainText("no edge to this agent");
  await expect(main(page).getByTestId("system-observe-canvas")).toHaveAttribute("data-path", "41");
  await expect(node(page, ledger)).toHaveAttribute("data-dimmed", "true");
  for (const id of ["entry", triage, billing, legal]) await expect(node(page, id)).toHaveAttribute("data-dimmed", "false");
  await expect(edge(page, "e2")).toHaveAttribute("data-dimmed", "true");
  await expect(edge(page, "e1")).toHaveAttribute("data-dimmed", "false");
  await expect(edge(page, "e1")).toHaveAttribute("data-count", "1");
  await expect(node(page, triage).getByTestId("system-node-numbers")).toContainText("1 run");
  await bothThemes(page, "Observe with a work selected");
  const dir = process.env.E2E_SCREENSHOT_DIR;
  if (dir) await screenshots(page, dir);

  // Closing the work brings back the whole system; going back selects it again.
  await chosen.getByTestId("system-work-detail-close").click();
  await expect(chosen).toHaveCount(0);
  await expect(node(page, ledger)).toHaveAttribute("data-dimmed", "false");
  await page.goBack();
  await expect(main(page).getByTestId("system-work-detail")).toHaveAttribute("data-trace", "41");
  await chosen.getByTestId("system-work-detail-close").click();

  // Nothing says the stream is live. A new work arrives through it: it heads the list, needs the member, the numbers
  // move, triage works and the entry's edge lights up.
  await expect(main(page).getByTestId("system-observe-stream")).toHaveCount(0);
  await expect(observe).toHaveAttribute("data-stream", "live");
  release = true;
  await expect(links.first()).toHaveAttribute("data-trace", "42", { timeout: 10_000 });
  await expect(links).toHaveCount(3);
  await expect(facet(page, "needs_you")).toContainText("1");
  await expect(numbers.getByTestId("summary-work-needs-person-value")).toHaveText("1");
  const row42 = workRow(page, 42);
  await expect(row42.getByTestId("system-work-status")).toHaveAttribute("data-status", "needs_person");
  await expect(row42).toContainText("Refund over the limit?");
  await facet(page, "needs_you").click();
  await expect(links).toHaveCount(1);
  await facet(page, "").click();
  await expect(node(page, triage).getByTestId("system-node-state")).toHaveAttribute("data-state", "working");
  await expect(node(page, "entry").getByTestId("system-node-state")).toHaveAttribute("data-state", "working");
  await expect(edge(page, "e0")).toHaveAttribute("data-lit", "true");
  await expect(main(page).getByTestId("system-observe-stream")).toHaveCount(0);

  // A hidden tab stops the stream; showing it again opens it.
  const setVisibility = (state: "hidden" | "visible") =>
    page.evaluate((value) => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
      document.dispatchEvent(new Event("visibilitychange"));
    }, state);
  await setVisibility("hidden");
  await expect(observe).toHaveAttribute("data-stream", "paused");
  const whileHidden = streams;
  await page.waitForTimeout(1_000);
  expect(streams).toBe(whileHidden);
  await setVisibility("visible");
  await expect(observe).toHaveAttribute("data-stream", "live");
  await expect.poll(() => streams).toBeGreaterThan(whileHidden);
  await expect(main(page).getByTestId("system-observe-stream")).toHaveCount(0);

  // The trace's page still opens from the list: its totals, the work of each agent, and the waterfall of its spans.
  await row41.getByTestId("system-work-trace").click();
  await page.waitForURL(`**${systemPath(project, slug)}/traces/41`);
  const trace = main(page).getByTestId("system-trace-page");
  await expect(trace.getByRole("heading", { level: 1 })).toContainText("41");
  await expect(trace.getByTestId("system-trace-summary")).toContainText("$0.0163");
  await expect(trace.locator(`[data-testid="system-trace-agent"][data-agent="${triage}"]`)).toContainText("$0.0123");
  await expect(trace.locator(`[data-testid="system-trace-agent"][data-agent="${billing}"]`)).toContainText("2 failed");
  const spans = trace.getByTestId("system-waterfall-span");
  await expect(spans).toHaveCount(5);
  expect(await spans.evaluateAll((rows) => rows.map((row) => row.getAttribute("data-kind")))).toEqual(["run", "message", "run", "kg", "message"]);
  await expect(spans.nth(0)).toContainText("sonnet");
  await expect(spans.nth(0).getByRole("link", { name: "Run 501" })).toHaveAttribute("href", `/p/${project}/runs/501`);
  await expect(spans.nth(1).getByRole("link", { name: "Message" })).toHaveAttribute("href", /\/chats\/7/);
  await expect(spans.nth(3)).toContainText("docs");
  await expect(spans.nth(3)).toContainText("3 results");
  await expect(spans.nth(4)).toHaveAttribute("data-off-design", "true");
  await expect(spans.nth(4)).toHaveAttribute("data-status", "refused");
  await expect(spans.nth(4)).toContainText("no edge to this agent");
  await expect(spans.nth(2)).toHaveAttribute("data-status", "error");
  await expect(trace.getByTestId("system-waterfall-bar")).toHaveCount(5);

  // Back to Observe.
  await trace.getByRole("link", { name: new RegExp(slug) }).first().click();
  await page.waitForURL(`**${systemPath(project, slug)}?mode=observe`);
  await expect(main(page).getByTestId("system-observe")).toBeVisible();
});
