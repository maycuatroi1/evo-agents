import type { Locator, Page } from "@playwright/test";

import { call } from "../src/lib/api/client";

import { signOut } from "./support/auth";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { bearerClient, machineToken, newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import {
  claimRun,
  COMMIT,
  dispatch,
  eventsOf,
  HARNESS_REPO,
  leaseCredentials,
  liveWorker,
  LONG_PLAN_RUN_PLAN,
  PLAN_RUN_BRANCH,
  PLAN_RUN_PLAN,
  planRunBody,
  planRunUnderway,
  reportState,
  RUN_PLAN,
  runControl,
  runOf,
  runPath,
  runRow,
  runToReview,
  say,
  seedLongPlanRunPlan,
  seedPlanRunPlan,
  seedRunPlan,
  sendEvents,
  startRun,
  tool,
  uploadDiff,
  workerInbox,
} from "./support/runs";
import { putSecretByApi, recordResponses, runCredentialsOf, secretValue } from "./support/secrets";
import { toast } from "./support/toast";

/**
 * The run page against the real API, with the spec as the worker: it claims the run, reports its states and sends its
 * events, messages and diff the way the daemon does (docs/workers.md). The page follows the run's stream, so a line the
 * worker sends shows within 2 seconds; a stream that drops comes back with Last-Event-ID and repeats no line; a stream
 * that cannot be used gives way to reading events. The owner approves, cancels, takes over, hands back and messages
 * the agent; anyone else reads only. The owner alone sees the credentials the run got, never a value. Pages render in
 * English.
 */
test.skip(isDeployed, "runs a fake worker against the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function logOf(page: Page) {
  return main(page).getByTestId("log-lines");
}

function line(page: Page, text: string) {
  return logOf(page).getByTestId("log-line").filter({ hasText: text });
}

function traceOf(page: Page) {
  return main(page).getByTestId("trace");
}

/** The run's page on its Raw log tab, which the URL keeps. */
function logPath(project: string, id: number) {
  return `${runPath(project, id)}?view=log`;
}

/** A run of step 2 that `me` dispatched, claimed by their worker, with the agent started. */
async function runningRun(me: Member, steps = ["2"]) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"));
  const runs = await dispatch(me, project, steps);
  const run = runs[0];
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  return { project, live, run, runs };
}

const DIFF = [
  "diff --git a/src/queue.ts b/src/queue.ts",
  "index 1111111..2222222 100644",
  "--- a/src/queue.ts",
  "+++ b/src/queue.ts",
  "@@ -1,3 +1,4 @@",
  " import { db } from './db';",
  "-export function claim() {}",
  "+export function claim(worker: string) {",
  "+  return db.lease(worker);",
  " }",
  "diff --git a/docs/queue.md b/docs/queue.md",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/docs/queue.md",
  "@@ -0,0 +1 @@",
  "+# Queue",
  "",
].join("\n");

test("the runs list leads to the run, whose log shows a new line within 2 seconds, filtered and searched", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, [
    say("Reading plan rollout."),
    tool("Bash", "pytest -q tests/hub/test_queue.py"),
    { kind: "system", body: { text: "verify: `pnpm test` exited 0 after 900 ms", exit_code: 0 } },
  ]);
  const { last_seq: total } = await runOf(me, project, run.id);

  await open(page, `/p/${project}/runs`);
  await runRow(page, run.id).getByTestId("run-link").click();
  await page.waitForURL(`**${runPath(project, run.id)}`);
  await expect(main(page).getByRole("heading", { level: 1 })).toContainText(`Run #${run.id}`);
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText(`#${run.id}`);
  await expect(main(page).getByTestId("run-state")).toHaveText("Running");
  await expect(main(page).locator('[data-testid="run-phase"][data-phase="running"]')).toHaveAttribute("data-status", "current");
  await expect(main(page).locator('[data-testid="run-phase"][data-phase="leased"]')).toHaveAttribute("data-status", "done");
  await expect(main(page).getByTestId("run-worker")).toHaveText(live.worker.name);

  // The Trace is the first tab: the agent's words, the tool call with its command, the moves and the verify line.
  const trace = traceOf(page);
  await expect(main(page).getByTestId("run-tab-trace")).toHaveAttribute("aria-selected", "true");
  // The tab bar scrolls sideways only: nothing in it reaches below it, so no vertical scrollbar shows beside the tabs.
  const tabList = main(page).getByRole("tablist", { name: "Session view" });
  expect(await tabList.evaluate((list) => list.scrollHeight - list.clientHeight)).toBe(0);
  await expect(trace).toHaveAttribute("role", "log");
  await expect(trace).toHaveAttribute("aria-live", "polite");
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "live");
  await expect(trace.getByTestId("trace-message")).toHaveText("Reading plan rollout.");
  await expect(trace.getByTestId("trace-tool")).toHaveAttribute("data-status", "in_progress");
  await expect(trace.getByTestId("trace-tool-running")).toBeVisible();
  await expect(trace.getByTestId("trace-tool-arg")).toHaveText("pytest -q tests/hub/test_queue.py");
  await expect(trace).toContainText("Leased to Running");
  await expect(trace).toContainText("verify: `pnpm test` exited 0 after 900 ms");
  await expect(trace.getByTestId("trace-typing")).toBeVisible();

  // What the worker sends now shows within 2 seconds, without a reload.
  await sendEvents(live, run.id, [say("Moving the fake clock forward 301 s.")]);
  await expect(trace.getByTestId("trace-message").filter({ hasText: "Moving the fake clock forward 301 s." })).toBeInViewport({ timeout: 2_000 });

  // The Raw log, one tab over and kept in the URL, has every event as a line.
  await main(page).getByRole("tab", { name: "Raw log" }).click();
  await expect(page).toHaveURL(new RegExp(`${runPath(project, run.id)}\\?view=log$`));
  const log = logOf(page);
  await expect(log).toHaveAttribute("role", "log");
  await expect(log).toHaveAttribute("aria-live", "polite");
  await expect(log.getByTestId("log-line")).toHaveCount(total + 1);
  await expect(line(page, "Reading plan rollout.")).toHaveAttribute("data-kind", "agent_message_chunk");
  await expect(line(page, "Leased to Running")).toHaveAttribute("data-kind", "state");
  await expect(line(page, "Moving the fake clock forward 301 s.")).toBeVisible();

  // Filters by group and the search, which highlights what it found.
  const facets = main(page).getByTestId("log-facets");
  await facets.getByRole("button", { name: /^Tools/ }).click();
  await expect(log.getByTestId("log-line")).toHaveCount(1);
  await expect(log.getByTestId("log-line")).toHaveAttribute("data-kind", "tool_call");
  await expect(log.getByTestId("log-line")).toContainText("Bash: pytest -q tests/hub/test_queue.py");
  await facets.getByRole("button", { name: /^All/ }).click();
  await main(page).getByTestId("log-search").fill("FAKE clock");
  await expect(log.getByTestId("log-line")).toHaveCount(1);
  await expect(log.locator("mark")).toHaveText("fake clock");
  await expect(main(page).getByTestId("log-count")).toHaveText(`1 of ${total + 1} lines`);
  await main(page).getByTestId("log-search").fill("no such words");
  await expect(main(page).getByTestId("log-empty")).toHaveText("No line matches the filter or the search.");
  await main(page).getByTestId("log-search").fill("");
  await expect(log.getByTestId("log-line")).toHaveCount(total + 1);

  // Pause holds the display while the stream goes on; Resume shows what came meanwhile.
  await main(page).getByTestId("log-pause").click();
  await expect(main(page).getByTestId("log-pause")).toHaveAttribute("aria-pressed", "true");
  await sendEvents(live, run.id, [say("Sent while the display was paused.")]);
  await expect(main(page).getByTestId("log-paused")).toHaveText("Display paused: 1 new line waits.");
  await expect(line(page, "Sent while the display was paused.")).toHaveCount(0);
  await main(page).getByTestId("log-pause").click();
  await expect(line(page, "Sent while the display was paused.")).toBeVisible();

  // Follow keeps the newest line in view; scrolling up turns it off, and the button turns it on again.
  await log.scrollIntoViewIfNeeded();
  await sendEvents(live, run.id, Array.from({ length: 60 }, (_, index) => say(`Progress note ${index + 1}`)));
  await expect(line(page, "Progress note 60")).toBeInViewport();
  const follow = main(page).getByTestId("log-follow");
  await expect(follow).toHaveAttribute("aria-pressed", "true");
  await log.hover();
  await page.mouse.wheel(0, -2_000);
  await expect(follow).toHaveAttribute("aria-pressed", "false");
  await sendEvents(live, run.id, [say("Arrived while not following.")]);
  await expect(line(page, "Arrived while not following.")).not.toBeInViewport();
  await follow.click();
  await expect(follow).toHaveAttribute("aria-pressed", "true");
  await expect(line(page, "Arrived while not following.")).toBeInViewport();
});

test("a dropped stream reconnects with Last-Event-ID and shows no line twice", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, [say("first"), say("second"), say("third")]);
  const before = await eventsOf(me, project, run.id);

  // The headers a request goes out with, as Chromium sends them (EventSource adds Last-Event-ID below what routes see).
  const lastEventIds: (string | null)[] = [];
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  cdp.on("Network.requestWillBeSentExtraInfo", (event) => {
    const headers = Object.fromEntries(Object.entries(event.headers).map(([key, value]) => [key.toLowerCase(), String(value)]));
    if (headers.accept === "text/event-stream") lastEventIds.push(headers["last-event-id"] ?? null);
  });

  // The first connection carries every event so far, the last one twice, then drops; the next one reaches the hub.
  let fulfilled = 0;
  await page.route(`**/v1/projects/${project}/runs/${run.id}/stream**`, async (route) => {
    if (fulfilled > 0) return route.continue();
    fulfilled += 1;
    const frames = before.events.map((event) => `id: ${event.seq}\ndata: ${JSON.stringify(event)}\n\n`);
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: `retry: 200\n\n${frames.join("")}${frames.at(-1)}`,
    });
  });

  await open(page, logPath(project, run.id));
  await expect.poll(() => lastEventIds.length).toBeGreaterThanOrEqual(1);
  expect(lastEventIds[0]).toBe(String(before.last_seq));

  await sendEvents(live, run.id, [say("after the reconnect")]);
  await expect(line(page, "after the reconnect")).toBeVisible({ timeout: 2_000 });
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "live");
  const seqs = await logOf(page).getByTestId("log-line").evaluateAll((rows) => rows.map((row) => row.getAttribute("data-seq")));
  expect(seqs).toEqual(Array.from({ length: before.last_seq + 1 }, (_, index) => String(index + 1)));
});

test("when the stream cannot be used, the page reads the events instead", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, [say("before the outage")]);
  const { last_seq: total } = await runOf(me, project, run.id);
  await page.route(`**/v1/projects/${project}/runs/${run.id}/stream**`, (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: '{"error":"unavailable","message":"no stream"}' }),
  );

  await open(page, logPath(project, run.id));
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "polling");
  await expect(main(page).getByTestId("log-paused")).toHaveText("The live stream is unavailable: the page reads new lines every 1.5 seconds.");
  await expect(logOf(page).getByTestId("log-line")).toHaveCount(total);
  await sendEvents(live, run.id, [say("read while polling")]);
  await expect(line(page, "read while polling")).toBeVisible({ timeout: 4_000 });
  await reportState(live, run.id, { state: "failed", error: "the agent stopped" });
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "ended", { timeout: 4_000 });
  await expect(main(page).getByTestId("run-state")).toHaveText("Failed");
  await expect(main(page).getByTestId("run-error-text")).toHaveText("the agent stopped");
});

test("Approve turns a run in review done, and the plan shows its step done", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await runToReview(live, run.id);

  await open(page, runPath(project, run.id));
  await expect(main(page).getByTestId("run-state")).toHaveText("Review");
  await expect(main(page).locator('[data-testid="run-phase"][data-phase="review"]')).toHaveAttribute("data-status", "current");
  await expect(main(page).getByTestId("run-note-review")).toBeVisible();
  const result = main(page).getByTestId("run-result");
  await expect(result.getByTestId("run-verify")).toContainText("exit 0");
  await expect(result.getByTestId("run-verify")).toContainText("pnpm test");
  await expect(result.getByTestId("run-commit")).toContainText(COMMIT.slice(0, 7));
  await expect(result.getByTestId("run-diffstat")).toContainText("2 files, +12 -3");
  await expect(result.getByTestId("run-evidence")).toContainText(COMMIT.slice(0, 7));
  await expect(main(page).getByTestId("run-composer")).toHaveCount(0); // a run in review takes no messages
  await expect(main(page).getByTestId("run-takeover")).toHaveCount(0);

  await main(page).getByTestId("run-approve").click();
  await expect(toast(page, `Run #${run.id} approved`)).toContainText(`Step 2 of ${RUN_PLAN} is done.`);
  await expect(main(page).getByTestId("run-state")).toHaveText("Done");
  await expect(main(page).locator('[data-testid="run-phase"][data-phase="done"]')).toHaveAttribute("data-status", "done");
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "ended");
  await expect(traceOf(page)).toContainText("Review to Done, by the owner");
  await expect(main(page).getByTestId("run-approve")).toHaveCount(0);
  await expect(main(page).getByTestId("run-rerun")).toBeVisible();
  expect((await runOf(me, project, run.id)).state).toBe("done");

  await open(page, `/p/${project}/plans/${RUN_PLAN}`);
  await expect(page.getByTestId("board-column-done").locator('[data-step="2"]')).toBeVisible();
});

test("Cancel stops a queued run at once, and asks the worker to stop a running one", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run, runs } = await runningRun(me, ["2", "4"]);
  const queued = runs[1];

  await open(page, runPath(project, queued.id));
  await expect(main(page).getByTestId("run-state")).toHaveText("Queued");
  await main(page).getByTestId("run-cancel").click();
  let dialog = page.getByTestId("cancel-run-dialog");
  await expect(dialog).toContainText("stops at once");
  await dialog.getByTestId("cancel-run-dialog-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(toast(page, `Run #${queued.id} cancelled`)).toBeVisible();
  await expect(main(page).getByTestId("run-state")).toHaveText("Cancelled");
  await expect(main(page).locator('[data-testid="run-phase"][data-status="stopped"]')).toHaveAttribute("data-phase", "queued");

  await open(page, runPath(project, run.id));
  await main(page).getByTestId("run-cancel").click();
  dialog = page.getByTestId("cancel-run-dialog");
  await expect(dialog).toContainText("next heartbeat");
  await dialog.getByTestId("cancel-run-dialog-confirm").click();
  await expect(toast(page, `Cancel of run #${run.id} asked`)).toContainText("next heartbeat");
  await expect(main(page).getByTestId("run-note-cancel")).toBeVisible();
  await expect(main(page).getByTestId("run-cancel")).toHaveCount(0);
  await expect(main(page).getByTestId("run-composer")).toHaveCount(0);
  expect(await runControl(live, run.id)).toMatchObject({ held: true, cancel: true });

  // The worker stops the agent and reports it: the page follows through the stream.
  await reportState(live, run.id, { state: "cancelled" });
  await expect(main(page).getByTestId("run-state")).toHaveText("Cancelled", { timeout: 4_000 });
  await expect(main(page).locator('[data-testid="run-phase"][data-status="stopped"]')).toHaveAttribute("data-phase", "running");
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "ended");
  await expect(main(page).getByTestId("run-rerun")).toBeVisible();
});

test("a message from the owner shows in the log and waits in the run's inbox", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await open(page, runPath(project, run.id));
  const composer = main(page).getByTestId("run-composer");
  await composer.getByTestId("run-composer-send").click();
  await expect(composer.getByTestId("run-composer-problem")).toHaveText("Type a message first.");

  const text = "Also cover the revoked-token case, với tiếng Việt.";
  await composer.getByTestId("run-composer-text").fill(text);
  await composer.getByTestId("run-composer-send").click();
  await expect(composer.getByTestId("run-composer-sent")).toContainText("Sent.");
  await expect(composer.getByTestId("run-composer-text")).toHaveValue("");
  await expect(traceOf(page).getByTestId("trace-item").filter({ hasText: text })).toHaveAttribute("data-type", "user");
  await expect(traceOf(page).getByTestId("trace-item").filter({ hasText: text })).toContainText("You");
  await expect(line(page, text)).toHaveAttribute("data-kind", "user_message");
  await expect(line(page, text)).toContainText(`${me.login}: ${text}`);

  expect(await workerInbox(live, run.id)).toEqual([expect.objectContaining({ text, sent_by: me.login })]);
  expect(await runControl(live, run.id)).toMatchObject({ inbox: 1 });
});

test("Take over shows the attach command and Remote Control, and Hand back follows", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await open(page, runPath(project, run.id));

  await main(page).getByTestId("run-takeover").click();
  const dialog = page.getByTestId("takeover-dialog");
  await expect(dialog.getByRole("heading", { name: `Take over run #${run.id}` })).toBeVisible();
  await expect(dialog.getByTestId("takeover-attach")).toContainText(`evo-agents worker attach ${run.id}`);
  await expect(dialog).toContainText(`On ${live.worker.name}, attach to the session`);
  await expect(dialog.getByTestId("takeover-remote-control")).toContainText(`evo-run-${run.id}`);
  await dialog.getByTestId("takeover-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(toast(page, `Takeover of run #${run.id} asked`)).toBeVisible();
  await expect(main(page).getByTestId("run-note-takeover")).toBeVisible();
  await expect(main(page).getByTestId("run-takeover")).toHaveCount(0);
  expect(await runControl(live, run.id)).toMatchObject({ takeover: true });

  await reportState(live, run.id, { state: "interactive" });
  await expect(main(page).getByTestId("run-state")).toHaveText("Interactive", { timeout: 4_000 });
  await expect(main(page).getByTestId("run-note-interactive")).toContainText(`evo-agents worker attach ${run.id}`);
  await main(page).getByTestId("run-handback").click();
  await expect(toast(page, `Hand back of run #${run.id} asked`)).toBeVisible();
  await expect(main(page).getByTestId("run-note-handback")).toBeVisible();
  expect(await runControl(live, run.id)).toMatchObject({ handback: true });
  await reportState(live, run.id, { state: "running" });
  await expect(main(page).getByTestId("run-state")).toHaveText("Running", { timeout: 4_000 });
  await expect(main(page).getByTestId("run-takeover")).toBeVisible();
});

test("someone other than the owner reads the run but gets no message box, no Take over and no Cancel", async ({
  page,
  member,
  admin,
  signInAs,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, [say("visible to every reader")]);
  const other = newAccount("colleague");
  await admin.grant(project, other.login, "writer", "internal");
  await signOut(page); // of the hub and the fake GitHub
  await signInAs(other);

  await open(page, runPath(project, run.id));
  await expect(traceOf(page).getByTestId("trace-message")).toHaveText("visible to every reader");
  await expect(main(page).getByTestId("run-note-owner")).toContainText(`Only ${me.login}, who dispatched this run`);
  await expect(main(page).getByTestId("run-composer")).toHaveCount(0);
  await expect(main(page).getByTestId("run-takeover")).toHaveCount(0);
  await expect(main(page).getByTestId("run-cancel")).toHaveCount(0);
  await expect(main(page).getByTestId("run-worker").getByRole("link")).toHaveCount(0); // another member's worker
});

test("the owner sees the credentials the run got, never a value, and nobody else sees them", async ({
  page,
  member,
  admin,
  signInAs,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const oauth = secretValue("oauth");
  const git = secretValue("git");
  await putSecretByApi(me, "claude-oauth", { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], value: oauth });
  await putSecretByApi(me, "github-org", { kind: "git", url_prefix: "https://github.com/example-org", projects: [project], value: git });
  const { live, run } = await runningRun(me);
  // The worker gets the values, as the daemon does right after its claim.
  const leased = await leaseCredentials(live, run.id);
  expect(Object.fromEntries(leased.leases.map((lease) => [lease.name, lease.value]))).toEqual({ "claude-oauth": oauth, "github-org": git });
  const received = recordResponses(page);

  await open(page, runPath(project, run.id));
  const card = main(page).getByTestId("run-credentials");
  await expect(card.getByRole("heading", { name: "Credentials" })).toBeVisible();
  await expect(card.getByTestId("run-lease-item")).toHaveCount(2);
  const env = card.getByTestId("run-lease-item").filter({ hasText: "claude-oauth" });
  await expect(env.getByTestId("run-lease-provider")).toHaveText("Your secret");
  await expect(env.getByTestId("run-lease-target")).toHaveText("CLAUDE_CODE_OAUTH_TOKEN");
  await expect(env.getByTestId("run-lease-issued")).toContainText(live.worker.name);
  await expect(env.getByTestId("run-lease-expires")).toHaveText("no end");
  await expect(env.getByTestId("run-lease-revoked")).toHaveText("not yet");
  await expect(env.getByTestId("run-lease-state")).toHaveText("Out");
  const repo = card.getByTestId("run-lease-item").filter({ hasText: "github-org" });
  await expect(repo.getByTestId("run-lease-target")).toHaveText("https://github.com/example-org/api");
  await expect(repo.getByTestId("run-lease-state")).toHaveText("Out");

  // The run ends: the hub takes its leases back, and the page shows it without a reload.
  await reportState(live, run.id, { state: "failed", error: "the agent stopped" });
  await expect(main(page).getByTestId("run-state")).toHaveText("Failed", { timeout: 4_000 });
  await expect(card.locator('[data-testid="run-lease-item"][data-state="revoked"]')).toHaveCount(2, { timeout: 10_000 });
  await expect(env.getByTestId("run-lease-state")).toHaveText("Revoked");
  await expect(env.getByTestId("run-lease-revoked")).not.toHaveText("not yet");

  const html = await page.content();
  expect(html).not.toContain(oauth);
  expect(html).not.toContain(git);
  for (const body of await received()) {
    expect(body.includes(oauth) || body.includes(git), "a response the browser received holds a value").toBe(false);
  }

  // Another writer of the project reads the run, but not what it got.
  const other = newAccount("colleague");
  await admin.grant(project, other.login, "writer", "internal");
  await signOut(page);
  await signInAs(other);
  await open(page, runPath(project, run.id));
  await expect(main(page).getByTestId("run-details")).toBeVisible();
  await expect(main(page).getByTestId("run-credentials")).toHaveCount(0);
  const refused = await runCredentialsOf(other, project, run.id);
  expect(refused.status).toBe(403);
  expect(JSON.stringify(refused.body)).toContain(`only ${me.login}, who dispatched run ${run.id}`);
});

/** A link out of the hub: its address, a new tab without opener or referrer, and a name that says it opens a tab. */
async function expectExternal(link: Locator, href: string, text: string) {
  await expect(link).toHaveAttribute("href", href);
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(link).toHaveAccessibleName(`${text} (opens in a new tab)`);
  await expect(link).toHaveText(text);
}

test("the repo and the commit lead to GitHub, and every address in the trace, the result and the credentials is a link", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await putSecretByApi(me, "github-org", { kind: "git", url_prefix: "https://github.com/example-org", projects: [project], value: secretValue("git") });
  const { live, run } = await runningRun(me);
  await leaseCredentials(live, run.id);
  const pr = "https://github.com/example-org/api/pull/7";
  await sendEvents(live, run.id, [
    say(`Opened ${pr} for the review.`),
    { kind: "tool_call", body: { toolCallId: "call-pr", title: "Bash", kind: "execute", status: "pending", rawInput: { command: "gh pr create --fill" } } },
    {
      kind: "tool_call_update",
      body: { toolCallId: "call-pr", status: "failed", rawOutput: { exitCode: 1 }, content: [{ type: "content", content: { type: "text", text: `${pr}\njavascript:alert(1)` } }] },
    },
  ]);
  await reportState(live, run.id, { state: "verifying" });
  await reportState(live, run.id, {
    state: "review",
    commit_sha: COMMIT,
    verify: [{ command: "curl -fsS https://ci.example.org/health", exit_code: 0, duration_ms: 900 }],
    summary: `Opened ${pr}.`,
  });

  await open(page, runPath(project, run.id));
  // The repo of the project, registered with its GitHub origin, and the commit on it.
  const details = main(page).getByTestId("run-details");
  await expectExternal(details.getByTestId("run-repo-link"), "https://github.com/example-org/api", "api");
  const result = main(page).getByTestId("run-result");
  await expectExternal(result.getByTestId("run-commit-sha"), `https://github.com/example-org/api/commit/${COMMIT}`, COMMIT.slice(0, 7));
  await expectExternal(result.getByTestId("run-verify").getByRole("link"), "https://ci.example.org/health", "https://ci.example.org/health");
  await expectExternal(result.getByTestId("run-evidence").getByRole("link", { name: /pull\/7/ }), pr, pr);

  // The trace: the agent's words and the command's output link the pull request; a javascript: line stays text.
  const trace = traceOf(page);
  await expect(trace.getByTestId("trace-message").getByRole("link", { name: /pull\/7/ })).toHaveAttribute("href", pr);
  const output = trace.getByTestId("trace-tool-output");
  await expectExternal(output.getByRole("link"), pr, pr);
  await expect(output).toContainText("javascript:alert(1)");
  await expect(main(page).locator('a[href^="javascript:"], a[href^="data:"]')).toHaveCount(0);

  // The repo the git lease answered for links to its page; the Secrets page is a link in the card's head.
  const credentials = main(page).getByTestId("run-credentials");
  const lease = credentials.getByTestId("run-lease-item").filter({ hasText: "github-org" });
  await expectExternal(lease.getByTestId("run-lease-repo-link"), "https://github.com/example-org/api", "https://github.com/example-org/api");
  await expect(credentials.getByTestId("run-credentials-secrets-link")).toHaveAttribute("href", "/secrets");
});

test("a plan run's repos and branches lead to their pages, a repo without an origin or on another forge says less", async ({ page, member, admin }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { run } = await planRunUnderway(me, project, uniqueName("links"));

  await open(page, runPath(project, run.id));
  const repos = main(page).getByTestId("run-repos");
  const api = repos.locator("li").filter({ hasText: "api" });
  await expectExternal(api.getByTestId("run-repo-link"), "https://github.com/example-org/api", "api");
  await expectExternal(api.getByTestId("run-branch"), `https://github.com/example-org/api/tree/${PLAN_RUN_BRANCH}`, PLAN_RUN_BRANCH);
  // The harness repo is not one of the project's, so the page has no origin for it: its name and branch stay text.
  const harness = repos.locator("li").filter({ hasText: HARNESS_REPO });
  await expect(harness.getByTestId("run-repo-name")).toHaveText(HARNESS_REPO);
  await expect(harness.getByRole("link")).toHaveCount(0);
  // The text of each repo is what it was: the words for screen readers live in the links' names.
  await expect(repos.locator("li")).toHaveText([`apion ${PLAN_RUN_BRANCH}`, `${HARNESS_REPO}on main`]);

  // On a forge the page does not know, the repo still leads to its page, and its branch is text.
  await admin.registerProject(project, { repos: [{ name: "api", origin: "git@git.example.net:team/api.git", default_branch: "main", path: "api" }] });
  await open(page, runPath(project, run.id));
  await expectExternal(api.getByTestId("run-repo-link"), "https://git.example.net/team/api", "api");
  await expect(api.getByTestId("run-branch")).not.toHaveAttribute("href");
  await expect(api.getByRole("link")).toHaveCount(1);

  // An origin the page cannot read as a web page (here a script) is never a link.
  await admin.registerProject(project, { repos: [{ name: "api", origin: "javascript:alert(document.domain)", default_branch: "main", path: "api" }] });
  await open(page, runPath(project, run.id));
  await expect(api.getByTestId("run-repo-name")).toHaveText("api");
  await expect(repos.getByRole("link")).toHaveCount(0);
  await expect(main(page).locator('a[href^="javascript:"]')).toHaveCount(0);
});

test("the run page says each thing once: no hint of when a card fills or who sees it, and an empty card is one line", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);

  await open(page, runPath(project, run.id));
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "live");
  // Usage and Result, with nothing yet, are their title and a short state.
  const usage = main(page).getByTestId("run-usage");
  await expect(usage.getByTestId("run-usage-empty")).toHaveText("Nothing reported yet");
  await expect(usage).toHaveText("UsageNothing reported yet");
  const result = main(page).getByTestId("run-result");
  await expect(result.getByTestId("run-result-empty")).toHaveText("None yet");
  await expect(result).toHaveText("ResultNone yet");
  // Credentials: its head links the Secrets page, with no sentence of who sees the card.
  const credentials = main(page).getByTestId("run-credentials");
  await expect(credentials.getByTestId("run-credentials-empty")).toHaveText("None yet.");
  await expect(credentials.getByRole("link", { name: "Secrets" })).toHaveAttribute("href", "/secrets");
  // The composer's bar says what the run is, nothing about who sees the box.
  await expect(main(page).getByTestId("run-composer-hint")).toHaveText(`Headless run on ${live.worker.name}.`);
  for (const words of ["Shows once the agent", "never a value. Only you see this", "Your secrets are on the", "Only you see this box"]) {
    await expect(main(page)).not.toContainText(words);
  }
});

test("a plan run's head names its plan once, with its title only when it says more than the id", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { run } = await planRunUnderway(me, project, uniqueName("head"));

  await open(page, runPath(project, run.id));
  const planLink = main(page).getByTestId("run-plan-link");
  await expect(planLink).toHaveText(PLAN_RUN_PLAN);
  await expect(planLink).toHaveAttribute("href", `/p/${project}/plans/${PLAN_RUN_PLAN}`);
  await expect(planLink.locator("..")).toHaveText(`Plan ${PLAN_RUN_PLAN}: ${planRunBody().title}`);
  await expect(main(page).getByTestId("run-plan-steps")).not.toContainText("As the hub holds the plan");
  await expect(main(page)).not.toContainText("Every step not done yet");

  // A plan whose title is its id: the head says the plan once.
  const plain = "plain-plan";
  const api = bearerClient(await machineToken(me));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: plain } },
      body: { body: { ...planRunBody(plain), title: plain }, area: "active" },
    }),
  );
  const { run: second } = await planRunUnderway(me, project, uniqueName("head"), { dispatch: { plan_id: plain } });
  await open(page, runPath(project, second.id));
  await expect(main(page).getByTestId("run-plan-link").locator("..")).toHaveText(`Plan ${plain}`);
});

test("the diff page shows the diff the worker uploaded, file by file, and downloads it", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await runToReview(live, run.id);
  await uploadDiff(live, run.id, DIFF);

  await open(page, runPath(project, run.id));
  await main(page).getByTestId("run-view-diff").click();
  await page.waitForURL(`**${runPath(project, run.id)}/diff`);
  await expect(main(page).getByRole("heading", { level: 1 })).toContainText(`Diff of run #${run.id}`);
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText("Diff");
  await expect(main(page).getByTestId("diff-files")).toContainText("2 files changed");
  const files = main(page).getByTestId("diff-file");
  await expect(files).toHaveCount(2);
  await expect(files.nth(0)).toHaveAttribute("data-path", "src/queue.ts");
  await expect(files.nth(0).getByTestId("diff-line").filter({ hasText: "return db.lease(worker);" })).toHaveAttribute("data-kind", "added");
  await expect(files.nth(0).getByTestId("diff-line").filter({ hasText: "export function claim() {}" })).toHaveAttribute("data-kind", "removed");
  await expect(files.nth(1)).toHaveAttribute("data-path", "docs/queue.md");
  await expect(files.nth(1)).toContainText("Added");

  const [download] = await Promise.all([page.waitForEvent("download"), main(page).getByTestId("diff-download").click()]);
  expect(download.suggestedFilename()).toBe(`run-${run.id}.diff`);

  await main(page).getByTestId("diff-back").click();
  await page.waitForURL(`**${runPath(project, run.id)}`);
});

test("a run without a diff, and a run that does not exist, say so", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run } = await runningRun(me);
  await open(page, `${runPath(project, run.id)}/diff`);
  await expect(main(page).getByTestId("diff-none")).toContainText("This run has no diff");
  await open(page, runPath(project, 999_999_999));
  await expect(main(page).getByTestId("state-not-found")).toContainText("Run #999999999 not found");
  await open(page, `/p/${project}/runs/not-a-number`);
  await expect(main(page).getByTestId("state-not-found")).toBeVisible();
});

test("past 2,000 lines the log renders only the rows in view", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, Array.from({ length: 2_050 }, (_, index) => say(`line number ${index + 1}`)));
  const { last_seq: total } = await runOf(me, project, run.id);

  await open(page, logPath(project, run.id));
  const log = logOf(page);
  await expect(main(page).getByTestId("log-count")).toHaveText(`${total.toLocaleString("en-US")} lines`, { timeout: 15_000 });
  await expect(log).toHaveAttribute("data-virtual", "true");
  expect(await log.getByTestId("log-line").count()).toBeLessThan(200);
  await log.scrollIntoViewIfNeeded();
  await expect(line(page, "line number 2050")).toBeInViewport();

  await sendEvents(live, run.id, [say("the newest line")]);
  await expect(line(page, "the newest line")).toBeInViewport({ timeout: 2_000 });

  // A search that leaves fewer lines renders them all again.
  await main(page).getByTestId("log-search").fill("line number 20");
  await expect(main(page).getByTestId("log-count")).toHaveText(`62 of ${(total + 1).toLocaleString("en-US")} lines`);
  await expect(log).not.toHaveAttribute("data-virtual", "true");
  await expect(log.getByTestId("log-line")).toHaveCount(62);
});

test("the Trace pairs each tool call with its result, opens a failed one, folds the thinking, and the usage card adds up", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  const output = Array.from({ length: 30 }, (_, index) => `test_${index + 1} FAILED`).join("\n");
  const text = (value: string) => ({ type: "text", text: value });
  await sendEvents(live, run.id, [
    { kind: "agent_thought_chunk", body: { content: text("The queue needs a lease column before claim can use it.") } },
    { kind: "agent_message_chunk", body: { content: text("I'll add the lease column, then run the tests.") } },
    {
      kind: "tool_call",
      body: {
        toolCallId: "toolu_edit",
        title: "Edit",
        kind: "edit",
        status: "pending",
        rawInput: { file_path: "src/queue.ts", old_string: "claim() {}", new_string: "claim(worker) {\n  return lease(worker);\n}" },
      },
    },
    { kind: "output", body: { raw: { type: "rate_limit_event" } } },
    { kind: "tool_call_update", body: { toolCallId: "toolu_edit", status: "completed", content: [{ type: "content", content: text("Updated src/queue.ts") }] } },
    { kind: "tool_call", body: { toolCallId: "toolu_test", title: "Bash", kind: "execute", status: "pending", rawInput: { command: "pytest -q", description: "Run the tests" } } },
    {
      kind: "tool_call_update",
      body: { toolCallId: "toolu_test", status: "failed", rawOutput: { exitCode: 1 }, content: [{ type: "content", content: text(output) }] },
    },
    { kind: "plan", body: { entries: [{ content: "Add the lease column", status: "completed" }, { content: "Make the tests pass", status: "in_progress" }] } },
    {
      kind: "usage_update",
      body: {
        usage: { input_tokens: 12, cache_read_input_tokens: 9000, cache_creation_input_tokens: 300, output_tokens: 240 },
        cost: { amount: 0.42, currency: "USD" },
      },
    },
  ]);

  await open(page, runPath(project, run.id));
  const trace = traceOf(page);
  const tools = trace.getByTestId("trace-tool");
  await expect(tools).toHaveCount(2);

  // The edit: its file, its diff stat, folded; opened, its change as a diff.
  const edit = tools.nth(0);
  await expect(edit).toHaveAttribute("data-status", "completed");
  await expect(edit.getByTestId("trace-tool-arg")).toHaveText("src/queue.ts");
  await expect(edit.getByTestId("trace-diffstat")).toContainText("+3 −1");
  await expect(edit).not.toHaveAttribute("open");
  await edit.getByTestId("trace-tool-arg").click();
  await expect(edit.getByTestId("trace-tool-change")).toContainText("+   return lease(worker);");

  // The failed command is open on its exit code, its output cut at 20 lines until asked.
  const failed = tools.nth(1);
  await expect(failed).toHaveAttribute("open", "");
  await expect(failed.getByTestId("trace-exit")).toHaveText("exit 1");
  await expect(trace.getByTestId("trace-item").filter({ hasText: "Run the tests" }).getByTestId("trace-tool-arg")).toHaveText("pytest -q");
  const result = failed.getByTestId("trace-tool-output");
  await expect(result).toContainText("test_20 FAILED");
  await expect(result).not.toContainText("test_21 FAILED");
  await result.getByRole("button", { name: "Show the full output (30 lines)" }).click();
  await expect(result).toContainText("test_30 FAILED");

  // The thinking folded over the agent's words, the plan as a checklist, the rate-limit event raw.
  const thought = trace.getByTestId("trace-thought");
  await expect(thought).toContainText(/^Thought (briefly|for)/);
  await thought.getByText(/^Thought/).click();
  await expect(thought).toContainText("The queue needs a lease column before claim can use it.");
  await expect(trace.getByTestId("trace-plan")).toContainText("Make the tests pass");
  await expect(trace.getByTestId("trace-raw")).toContainText("1 runtime event");
  await expect(trace).not.toContainText("cache_read_input_tokens");

  // The usage card adds up the report: the tokens in four parts, the cost as the runtime reported it.
  const usage = main(page).getByTestId("run-usage");
  await expect(usage.getByTestId("run-usage-total")).toHaveText("9,252");
  await expect(usage.getByTestId("run-usage-cost")).toHaveText("$0.42 as reported");
  await expect(usage.getByRole("img")).toHaveAccessibleName("Cache read 97.3 percent, input 0.1, output 2.6, reasoning 0");
  // The cost is said once, on its line; the note names the cache writes alone.
  await expect(usage.getByTestId("run-usage-note")).toHaveText("Cache write 300.");

  // The tab is kept in the URL: the Raw log comes back on a reload, and the Trace takes the parameter away.
  await main(page).getByRole("tab", { name: "Raw log" }).click();
  await page.reload();
  await expect(main(page).getByTestId("run-tab-log")).toHaveAttribute("aria-selected", "true");
  await main(page).getByTestId("run-tab-trace").click();
  await expect(page).toHaveURL(new RegExp(`${runPath(project, run.id)}$`));
});

test("a plan run's trace says what the agent asked, and Answer leads to the decision's card", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { run, decision } = await planRunUnderway(me, project, uniqueName("studio"), { waiting: true });

  await open(page, runPath(project, run.id));
  await expect(main(page).locator('[data-testid="run-phase"][data-phase="running"]')).toHaveAttribute("data-tone", "waiting");
  const ask = traceOf(page).getByTestId("trace-item").filter({ hasText: "Asked you" });
  await expect(ask).toContainText("Deploy the plan-runs build to staging now?");
  await expect(ask.getByTestId("trace-ask-link")).toHaveAttribute("href", `#run-decision-${decision}`);
  await ask.getByTestId("trace-ask-link").click();
  await expect(page).toHaveURL(new RegExp(`#run-decision-${decision}$`));
  const card = main(page).locator(`#run-decision-${decision}`);
  await expect(card).toBeInViewport();

  await card.getByTestId("decision-send").click();
  await expect(traceOf(page).getByTestId("trace-item").filter({ hasText: `You answered decision #${decision}` })).toBeVisible();
});

test("past 500 items the Trace renders only the ones in view, follows the newest, and offers the way back to it", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(
    live,
    run.id,
    Array.from({ length: 520 }, (_, index) => ({
      kind: "tool_call",
      body: { toolCallId: `call-${index + 1}`, title: "Read", kind: "read", status: "completed", rawInput: { file_path: `src/file-${index + 1}.ts` } },
    })),
  );

  await open(page, runPath(project, run.id));
  const trace = traceOf(page);
  await expect(trace).toHaveAttribute("data-virtual", "true", { timeout: 15_000 });
  expect(await trace.getByTestId("trace-item").count()).toBeLessThan(120);
  await trace.scrollIntoViewIfNeeded();
  await expect(trace.getByTestId("trace-tool-arg").filter({ hasText: "src/file-520.ts" })).toBeInViewport();

  await sendEvents(live, run.id, [say("the newest item")]);
  await expect(trace.getByTestId("trace-message").filter({ hasText: "the newest item" })).toBeInViewport({ timeout: 2_000 });

  // Scrolled up, the trace stays where the person put it, and Jump to the latest takes them back.
  await trace.hover();
  await page.mouse.wheel(0, -3_000);
  await expect(trace).toHaveAttribute("data-follow", "false");
  await sendEvents(live, run.id, [say("arrived while reading")]);
  await expect(trace.getByTestId("trace-message").filter({ hasText: "arrived while reading" })).toHaveCount(0); // not rendered: out of view
  await main(page).getByTestId("trace-latest").click();
  await expect(trace.getByTestId("trace-message").filter({ hasText: "arrived while reading" })).toBeInViewport();
  await expect(trace).toHaveAttribute("data-follow", "true");
});

test("from 1280 px the session and the side column scroll on their own, as tall as the window and level at the bottom", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedLongPlanRunPlan(me, project);
  await putSecretByApi(me, "claude-oauth", { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], value: secretValue("oauth") });
  await putSecretByApi(me, "github-org", { kind: "git", url_prefix: "https://github.com/example-org", projects: [project], value: secretValue("git") });
  // A plan run of eleven steps that waits for its owner and got credentials: its side column is longer than the window.
  const { live, run } = await planRunUnderway(me, project, uniqueName("split"), { waiting: true, dispatch: { plan_id: LONG_PLAN_RUN_PLAN } });
  await leaseCredentials(live, run.id);
  await sendEvents(live, run.id, Array.from({ length: 40 }, (_, index) => say(`Progress note ${index + 1}`)));

  const aside = main(page).getByTestId("run-aside");
  const session = main(page).getByTestId("run-log");
  const bottom = async (locator: ReturnType<typeof main>) => {
    const box = (await locator.boundingBox())!;
    return box.y + box.height;
  };
  for (const size of [
    { width: 1440, height: 900 },
    { width: 1920, height: 1080 },
  ]) {
    await page.setViewportSize(size);
    await open(page, runPath(project, run.id));
    await expect(aside.getByTestId("run-decision")).toBeVisible();
    await expect(aside.getByTestId("run-plan-step")).toHaveCount(11);
    await expect(aside.getByTestId("run-lease-item")).toHaveCount(2);
    await expect(traceOf(page).getByTestId("trace-message")).toContainText("Progress note 40");

    // Scrolled to the bottom of the page, the session card ends on the window's bottom gutter, with no space under its
    // trace: the trace reaches down to the composer.
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    const sessionBottom = await bottom(session);
    expect(sessionBottom, `at ${size.width} px the session card ends in the window`).toBeLessThanOrEqual(size.height);
    expect(Math.abs(size.height - sessionBottom - 24), `at ${size.width} px only the 24 px gutter is under the session card`).toBeLessThanOrEqual(1);
    const composerTop = (await main(page).getByTestId("run-composer").boundingBox())!.y;
    expect(Math.abs(composerTop - (await bottom(traceOf(page)))), `at ${size.width} px the trace reaches the composer`).toBeLessThanOrEqual(1);

    // The side column scrolls inside itself; it is a named region that Tab reaches and the keys scroll.
    expect(await aside.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeGreaterThan(0);
    await expect(aside).toHaveRole("region");
    await expect(aside).toHaveAccessibleName(`About run #${run.id}`);
    await main(page).focus();
    for (let presses = 0; presses < 40 && !(await aside.evaluate((element) => element === document.activeElement)); presses++) {
      await page.keyboard.press("Tab");
    }
    await expect(aside).toBeFocused();
    await page.keyboard.press("End");
    await expect.poll(() => aside.evaluate((element) => Math.ceil(element.scrollTop + element.clientHeight) >= element.scrollHeight)).toBe(true);
    // At its end its last card, Credentials, ends level with the session card, both in the window, and the page has
    // not moved.
    const credentialsBottom = await bottom(aside.getByTestId("run-credentials"));
    expect(Math.abs(credentialsBottom - (await bottom(session))), `at ${size.width} px the two regions end level`).toBeLessThanOrEqual(1);
    expect(credentialsBottom).toBeLessThanOrEqual(size.height);
    // They start level too: the decision at the top of the side column, beside the session card's tabs.
    const sessionTop = (await session.boundingBox())!.y;
    await aside.evaluate((element) => element.scrollTo({ top: 0 }));
    expect(Math.abs((await main(page).getByTestId("run-decisions").boundingBox())!.y - sessionTop)).toBeLessThanOrEqual(1);
  }

  // Below 1280 px one column, in the order of before: the decision, then the side cards, then the session card. The
  // side column does not scroll there, so Tab does not stop on it.
  await page.setViewportSize({ width: 1024, height: 900 });
  await open(page, runPath(project, run.id));
  await expect(aside.getByTestId("run-plan-step")).toHaveCount(11);
  const top = async (testId: string) => (await main(page).getByTestId(testId).boundingBox())!.y;
  expect(await top("run-decisions")).toBeLessThan(await top("run-side"));
  expect(await top("run-side")).toBeLessThan(await top("run-log"));
  await expect(aside).not.toHaveAttribute("tabindex");
  expect(await aside.evaluate((element) => element.scrollHeight - element.clientHeight)).toBe(0);
});

test.describe("in Vietnamese", () => {
  test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

  test("the run page keeps the English terms and says the rest in Vietnamese", async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const { project, run } = await runningRun(me);
    await open(page, runPath(project, run.id));
    await expect(main(page).getByRole("heading", { level: 1 })).toContainText(`Run #${run.id}`);
    await expect(main(page).getByTestId("run-state")).toHaveText("Đang chạy");
    await expect(main(page).getByTestId("run-takeover")).toHaveText("Tiếp quản");
    await expect(main(page).getByTestId("log-status")).toHaveText("Trực tiếp");
    await expect(traceOf(page)).toContainText("Đã nhận sang Đang chạy, do worker");
    await expect(main(page).getByRole("tab", { name: "Log thô" })).toBeVisible();
    await expect(main(page).getByTestId("run-composer")).toContainText("Nhắn cho agent");
  });
});
