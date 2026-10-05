import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, reportState, runOf, runPath, seedRunPlan, workerHeartbeat } from "./support/runs";
import { ageSessions, fakeTerminal, startFakeTerminal, terminalCloseCode, watchCsp } from "./support/terminal";

/**
 * The Terminal tab of a run's page against the real API and its relay (docs/workers.md, Terminal), in Chromium and in
 * Firefox. The stack plays the worker's end: once a browser waits it connects with the worker's token over a real
 * PTY, whose line discipline echoes what is typed and whose program answers each line and each resize (e2e/hub_stack.py,
 * FakeTerminal). The page opens the websocket on its own origin, so the web forwards it to the API as a reverse proxy
 * would, under connect-src 'self'; no page here logs a CSP violation. Only the owner of the run and of its worker sees
 * the tab, and the hub closes anyone else's socket with 4403.
 */
test.skip(isDeployed, "drives a fake worker against the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function panelOf(page: Page) {
  return main(page).getByTestId("terminal-panel");
}

/** A run of step 2 that `me` dispatched in interactive mode, held by their worker that allows the web terminal. */
async function interactiveRun(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"), 1, { terminal: true });
  const [run] = await dispatch(me, project, ["2"], { mode: "interactive" });
  expect((await claimRun(live))?.id).toBe(run.id);
  await workerHeartbeat(live, [run.id]);
  await reportState(live, run.id, { state: "interactive", session_id: "3f2a9c1e-0000-4000-8000-000000000017" });
  return { project, live, run };
}

async function openTerminalTab(page: Page, project: string, runId: number) {
  await open(page, runPath(project, runId));
  const tab = main(page).getByRole("tab", { name: "Terminal" });
  await expect(tab).toBeVisible();
  await tab.click();
  await expect(tab).toHaveAttribute("aria-selected", "true");
  return panelOf(page);
}

test("the owner connects the terminal: keys echo through the worker's PTY, a resize reaches it, Disconnect ends it", async ({
  page,
  member,
}) => {
  const violations = await watchCsp(page);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await interactiveRun(me);

  const panel = await openTerminalTab(page, project, run.id);
  await expect(panel.getByTestId("terminal-status")).toHaveText("Not connected");
  await expect(panel.getByTestId("terminal-intro")).toContainText(`The agent's TUI runs in tmux on ${live.worker.name}`);
  await expectNoSeriousViolations(page, "the Terminal tab before Connect");
  await panel.getByTestId("terminal-connect").click();

  // The hub holds the browser until the worker's end connects; nothing typed goes anywhere meanwhile.
  await expect(panel.getByTestId("terminal-status")).toHaveText(`Waiting for ${live.worker.name}`);
  await expect(panel.getByTestId("terminal-message")).toContainText(`Waiting for ${live.worker.name} to connect its end`);
  await startFakeTerminal(run.id, live.token);
  await expect(panel.getByTestId("terminal-status")).toHaveText("Connected");
  const screen = panel.getByTestId("terminal-screen");
  await expect(screen).toContainText(`fake worker terminal of run ${run.id}`);
  const first = (await fakeTerminal(run.id)).sizes[0];
  await expect(panel.getByTestId("terminal-size")).toHaveText(`${first[0]} × ${first[1]}`);
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousViolations(page, `the live terminal in ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });

  // Focus is in the terminal once it is live: the PTY echoes each key, and its program answers the line.
  await page.keyboard.type("hello");
  await expect(screen).toContainText("$ hello");
  await page.keyboard.press("Enter");
  await expect(screen).toContainText("echo: hello");
  await page.keyboard.insertText("xin chào tiếng Việt");
  await page.keyboard.press("Enter");
  await expect(screen).toContainText("echo: xin chào tiếng Việt");
  expect((await fakeTerminal(run.id)).lines).toEqual(["hello", "xin chào tiếng Việt"]);

  // A phone-wide window makes a narrower, taller terminal; the worker gets each new size as a resize frame, the last
  // one being the size the page shows once the layout settles.
  await page.setViewportSize({ width: 600, height: 900 });
  const settled = async () => {
    const sizes = (await fakeTerminal(run.id)).sizes;
    const [cols, rows] = sizes.at(-1)!;
    const shown = await panel.getByTestId("terminal-size").textContent();
    const printed = (await screen.textContent()) ?? "";
    return sizes.length > 1 && cols < first[0] && shown === `${cols} × ${rows}` && printed.includes(`size: ${cols}x${rows}`);
  };
  await expect.poll(settled, { timeout: 10_000 }).toBe(true);

  // The Log tab keeps the session; back on the Terminal tab it is still live.
  await main(page).getByRole("tab", { name: "Log" }).click();
  await expect(main(page).getByTestId("log-lines")).toBeVisible();
  await main(page).getByRole("tab", { name: "Terminal" }).click();
  await expect(panel.getByTestId("terminal-status")).toHaveText("Connected");

  // A second page of the same owner is refused while this one holds the terminal (4409), and says why.
  const second = await page.context().newPage();
  const secondViolations = await watchCsp(second);
  const secondPanel = await openTerminalTab(second, project, run.id);
  await secondPanel.getByTestId("terminal-connect").click();
  await expect(secondPanel).toHaveAttribute("data-close-code", "4409");
  await expect(secondPanel.getByTestId("terminal-message")).toContainText("open in another browser or tab");
  await expect(secondPanel.getByTestId("terminal-message")).toContainText(`The hub says: the terminal of run ${run.id} is open in another browser`);
  await second.close();

  await panel.getByTestId("terminal-disconnect").click();
  await expect(panel.getByTestId("terminal-status")).toHaveText("Closed");
  await expect(panel.getByTestId("terminal-message")).toContainText(`The agent's TUI keeps running in tmux on ${live.worker.name}`);
  await expect.poll(async () => (await fakeTerminal(run.id)).closed).toBe(1000);
  await expect(panel.getByTestId("terminal-connect")).toHaveText("Connect again");

  expect(violations).toEqual([]);
  expect(secondViolations).toEqual([]);
});

test("someone other than the owner sees no Terminal tab, and the hub closes their socket with 4403", async ({
  page,
  member,
  admin,
  signInAs,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run } = await interactiveRun(me);
  const other = newAccount("colleague");
  await admin.grant(project, other.login, "writer", "internal");
  await page.context().clearCookies();
  await signInAs(other);
  const violations = await watchCsp(page);

  await open(page, runPath(project, run.id));
  await expect(main(page).getByRole("heading", { level: 1 })).toContainText(`Run #${run.id}`);
  await expect(main(page).getByTestId("log-lines")).toBeVisible();
  await expect(main(page).getByRole("tab")).toHaveCount(0);
  await expect(main(page).getByTestId("terminal-panel")).toHaveCount(0);

  const refused = await terminalCloseCode(page, project, run.id);
  expect(refused.code).toBe(4403);
  expect(refused.reason).toContain(`only ${me.login}, who dispatched run ${run.id}, may open its terminal`);
  expect((await runOf(me, project, run.id)).state).toBe("interactive");
  expect(violations).toEqual([]);
});

test("a sign-in older than 12 hours is asked to sign in again, and the hub refuses it with 4403", async ({ page, member }) => {
  const violations = await watchCsp(page);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run } = await interactiveRun(me);
  expect(await ageSessions(me.login, 13)).toBeGreaterThan(0);

  const panel = await openTerminalTab(page, project, run.id);
  await expect(panel.getByTestId("terminal-message")).toContainText("You signed in 13 hours ago");
  await expect(panel.getByTestId("terminal-connect")).toHaveCount(0);
  await expect(panel.getByTestId("terminal-sign-in")).toBeVisible();

  const refused = await terminalCloseCode(page, project, run.id);
  expect(refused).toEqual({ code: 4403, reason: "the web session is older than 12 hours: sign in again" });

  await panel.getByTestId("terminal-sign-in").click();
  await page.waitForURL("**/login");
  expect(violations).toEqual([]);
});
