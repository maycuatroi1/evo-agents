import type { Page } from "@playwright/test";

import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import {
  claimRun,
  COMMIT,
  dispatch,
  eventsOf,
  liveWorker,
  reportState,
  RUN_PLAN,
  runControl,
  runOf,
  runPath,
  runRow,
  runToReview,
  say,
  seedRunPlan,
  sendEvents,
  startRun,
  tool,
  uploadDiff,
  workerInbox,
} from "./support/runs";
import { toast } from "./support/toast";

/**
 * The run page against the real API, with the spec as the worker: it claims the run, reports its states and sends its
 * events, messages and diff the way the daemon does (docs/workers.md). The page follows the run's stream, so a line the
 * worker sends shows within 2 seconds; a stream that drops comes back with Last-Event-ID and repeats no line; a stream
 * that cannot be used gives way to reading events. The owner approves, cancels, takes over, hands back and messages
 * the agent; anyone else reads only. Pages render in English.
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

  const log = logOf(page);
  await expect(log).toHaveAttribute("role", "log");
  await expect(log).toHaveAttribute("aria-live", "polite");
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "live");
  await expect(log.getByTestId("log-line")).toHaveCount(total);
  await expect(line(page, "Reading plan rollout.")).toHaveAttribute("data-kind", "agent_message_chunk");
  await expect(line(page, "Leased to Running")).toHaveAttribute("data-kind", "state");

  // A line the worker sends now shows within 2 seconds, without a reload.
  await sendEvents(live, run.id, [say("Moving the fake clock forward 301 s.")]);
  await expect(line(page, "Moving the fake clock forward 301 s.")).toBeVisible({ timeout: 2_000 });

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

  await open(page, runPath(project, run.id));
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

  await open(page, runPath(project, run.id));
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
  await expect(line(page, "Review to Done, by the owner")).toBeVisible();
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
  await page.context().clearCookies(); // signed out of the hub and the fake GitHub
  await signInAs(other);

  await open(page, runPath(project, run.id));
  await expect(line(page, "visible to every reader")).toBeVisible();
  await expect(main(page).getByTestId("run-note-owner")).toContainText(`Only ${me.login}, who dispatched this run`);
  await expect(main(page).getByTestId("run-composer")).toHaveCount(0);
  await expect(main(page).getByTestId("run-takeover")).toHaveCount(0);
  await expect(main(page).getByTestId("run-cancel")).toHaveCount(0);
  await expect(main(page).getByTestId("run-worker").getByRole("link")).toHaveCount(0); // another member's worker
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

  await open(page, runPath(project, run.id));
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
    await expect(line(page, "Đã nhận sang Đang chạy, do worker")).toBeVisible();
    await expect(main(page).getByTestId("run-composer")).toContainText("Nhắn cho agent");
  });
});
