import type { Locator, Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { signOut } from "./support/auth";
import {
  browserCloseCode,
  endFakeBrowser,
  fakeBrowser,
  inputsOf,
  LUMEN,
  NORTHWIND,
  PAGE_SIZE,
  pictureBox,
  shownTab,
  startFakeBrowser,
} from "./support/browser-view";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, reportState, runControl, runOf, runPath, seedRunPlan, workerHeartbeat } from "./support/runs";
import { watchCsp } from "./support/terminal";

/**
 * The Browser tab of a run dispatched with a browser (plan agent-browser, criterion a6), against the real API and its
 * relay. The stack plays the worker's end (e2e/fake_browser.py): it connects with the worker's token as the daemon's
 * broker does, shows two tabs whose screens are stills of one colour (northwind blue, lumen amber), takes its mode from
 * the run's state and records each event and ACK the page sends. The page opens the websocket on its own origin under
 * connect-src 'self', and no page here logs a CSP violation. The agent drives and the dispatcher watches and picks the
 * tab watched; Take control (the run's takeover) gives the person the mouse, the keys, text an input method composed,
 * a paste and the URL box, and Hand back returns them. The view says something of its connection only in trouble.
 */
test.skip(isDeployed, "drives a fake worker end against the local stack");

const SESSION = "3f2a9c1e-0000-4000-8000-000000000031";

/** The runs whose fake end a test of this worker started: each closes when its test ends, so none stays attached. */
const started = new Set<number>();

test.afterEach(async () => {
  for (const runId of started) await endFakeBrowser(runId, "close");
  started.clear();
});

function main(page: Page) {
  return page.locator("#main");
}

/** A run of step 2 that `me` dispatched with a browser, running on their worker, whose browser's end is connected. */
async function browserRun(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"), 1, { report: { browser: true } });
  const [run] = await dispatch(me, project, ["2"], { browser: true });
  expect(run.browser).toBe(true);
  expect((await claimRun(live))?.id).toBe(run.id);
  await workerHeartbeat(live, [run.id], { browser: true });
  await reportState(live, run.id, { state: "running", session_id: SESSION });
  await startFakeBrowser(run.id, live.token);
  started.add(run.id);
  return { project, live, run };
}

async function openBrowserTab(page: Page, path: string, name = "Browser") {
  await open(page, path);
  const tab = main(page).getByRole("tab", { name, exact: true });
  await expect(tab).toBeVisible();
  if ((await tab.getAttribute("aria-selected")) !== "true") await tab.click();
  await expect(tab).toHaveAttribute("aria-selected", "true");
  return main(page).getByTestId("browser-panel");
}

/** The view is live and shows `tab`'s still, drawn from a frame the page acknowledged. */
async function showing(panel: Locator, runId: number, tab: "northwind" | "lumen") {
  await expect(panel).toHaveAttribute("data-status", "live");
  await expect.poll(() => shownTab(panel.getByTestId("browser-canvas"))).toBe(tab);
  await expect
    .poll(async () => {
      const fake = await fakeBrowser(runId);
      const last = fake.screens.at(-1);
      return last !== undefined && last.tab === tab && fake.acks.includes(last.seq);
    })
    .toBe(true);
}

async function noSidewaysScroll(page: Page, label: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, `${label} scrolls sideways`).toBeLessThanOrEqual(0);
}

test("the dispatcher watches the agent's browser and picks its tab; trouble is said, and the view comes back", async ({ page, member }) => {
  test.setTimeout(60_000);
  const violations = await watchCsp(page);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await browserRun(me);

  const panel = await openBrowserTab(page, runPath(project, run.id));
  await showing(panel, run.id, "northwind");
  await expect(panel).toHaveAttribute("data-mode", "agent");
  await expect(panel).toHaveAttribute("data-worker", "true");
  expect((await fakeBrowser(run.id)).views).toEqual([{ viewing: true, login: false }]);
  // Nothing is said while all is well: no banner, no placeholder, the hint below the picture.
  await expect(panel.getByTestId("browser-message")).toHaveCount(0);
  await expect(panel.getByTestId("browser-placeholder")).toHaveCount(0);
  await expect(panel.getByTestId("browser-hint")).toHaveText("The agent drives this browser. Take control to use it.");
  await expect(panel.getByTestId("browser-canvas")).toHaveAccessibleName(`Browser of run #${run.id}: ${NORTHWIND.title}`);
  // The agent drives: the address shows, read-only, and back, forward and reload wait for the person to take control.
  const url = panel.getByTestId("browser-url");
  await expect(url).toHaveValue(NORTHWIND.url);
  await expect(url).toHaveAttribute("readonly", "");
  for (const id of ["browser-back", "browser-forward", "browser-reload"]) await expect(panel.getByTestId(id)).toBeDisabled();
  await expect(panel.getByTestId("browser-keys")).toHaveCount(0);
  await expect(panel.getByTestId("browser-takeover")).toHaveText("Take control");

  // A click on the picture reaches nothing while the agent drives; picking a tab shows it.
  const box = await pictureBox(panel.getByTestId("browser-canvas"));
  await page.mouse.click(box.left + box.width / 2, box.top + box.height / 2);
  const tabs = panel.getByTestId("browser-tab");
  await expect(tabs).toHaveText([NORTHWIND.title, LUMEN.title]);
  await expect(tabs.first()).toHaveAttribute("aria-current", "true");
  await tabs.nth(1).click();
  await showing(panel, run.id, "lumen");
  await expect(url).toHaveValue(LUMEN.url);
  await expect(tabs.nth(1)).toHaveAttribute("aria-current", "true");
  expect((await fakeBrowser(run.id)).inputs).toEqual([{ type: "tab", id: LUMEN.id }]);

  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousViolations(page, `the Browser tab watching the agent in ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 800, height: 900 });
  await showing(panel, run.id, "lumen");
  await noSidewaysScroll(page, "the Browser tab at 800 px");

  // The worker's end drops: the view says so, and comes back by itself once it reconnects.
  await endFakeBrowser(run.id, "drop");
  const message = panel.getByTestId("browser-message");
  await expect(message).toHaveText(`${live.worker.name} dropped its end of the browser. The view comes back when it reconnects.`);
  await expect(panel).not.toHaveAttribute("data-worker");
  await endFakeBrowser(run.id, "resume");
  await expect(panel).toHaveAttribute("data-worker", "true");
  await expect(message).toHaveCount(0);
  await expect.poll(async () => (await fakeBrowser(run.id)).attaches).toBe(2);
  await showing(panel, run.id, "lumen");

  // One view a run: a second window takes it, and this one offers to take it back.
  const second = await page.context().newPage();
  const secondViolations = await watchCsp(second);
  const secondPanel = await openBrowserTab(second, runPath(project, run.id));
  await showing(secondPanel, run.id, "lumen");
  await expect(panel).toHaveAttribute("data-close-code", "4409");
  await expect(message).toContainText("This browser is now open in another window or tab.");
  await second.close();
  await panel.getByTestId("browser-reconnect").click();
  await showing(panel, run.id, "lumen");
  await expect(message).toHaveCount(0);

  // The run's browser closes: the view ends and says why.
  await endFakeBrowser(run.id, "close");
  await expect(panel).toHaveAttribute("data-status", "closed");
  await expect(panel).toHaveAttribute("data-close-code", "1000");
  await expect(message).toContainText(`The browser view ended: ${live.worker.name} closed its end.`);
  await expect(message).toContainText("The hub says: the run's browser closed");
  await expect(panel.getByTestId("browser-reconnect")).toHaveText("Connect again");

  expect(violations).toEqual([]);
  expect(secondViolations).toEqual([]);
});

test("Take control gives the dispatcher the mouse, keys, composed text, a paste and the URL box; Hand back returns them", async ({
  page,
  member,
}) => {
  test.setTimeout(90_000);
  const violations = await watchCsp(page);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await browserRun(me);

  // ?view=browser opens the page on the Browser tab.
  const panel = await openBrowserTab(page, `${runPath(project, run.id)}?view=browser`);
  await showing(panel, run.id, "northwind");
  await panel.getByTestId("browser-takeover").click();
  await expect(panel.getByTestId("browser-hint")).toHaveText("The agent stops at the end of its turn, then the browser is yours.");
  expect(await runControl(live, run.id)).toMatchObject({ takeover: true });
  // The agent's turn ends and the worker hands the run over: the broker says human, and the person drives.
  await reportState(live, run.id, { state: "interactive", session_id: SESSION });
  await expect(panel).toHaveAttribute("data-mode", "human");
  await expect(panel.getByTestId("browser-screen")).toHaveAttribute("data-human", "true");
  await expect(panel.getByTestId("browser-hint")).toHaveText("You drive this browser. Press Esc, then Tab, to leave it.");
  await expect(panel.getByTestId("browser-handback")).toHaveText("Hand back");
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousViolations(page, `the Browser tab while the person drives in ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });

  // A click lands on the page's point under it, and puts the keys in the view.
  const canvas = panel.getByTestId("browser-canvas");
  const box = await pictureBox(canvas);
  await page.mouse.click(box.left + box.width / 2, box.top + box.height / 2);
  const keys = panel.getByTestId("browser-keys");
  await expect(keys).toBeFocused();
  await expect.poll(async () => (await inputsOf(run.id, "mouse")).filter((event) => event.action !== "move").length).toBe(2);
  const [down, up] = (await inputsOf(run.id, "mouse")).filter((event) => event.action !== "move");
  expect(down).toMatchObject({ action: "down", button: "left", clicks: 1 });
  expect(up).toMatchObject({ action: "up", button: "left" });
  for (const event of [down, up]) {
    expect(Math.abs((event.x as number) - PAGE_SIZE.width / 2)).toBeLessThan(4);
    expect(Math.abs((event.y as number) - PAGE_SIZE.height / 2)).toBeLessThan(4);
  }

  // Keys go as keys; the app's own single keys (G then I, ?, D, /) are the remote page's now.
  const here = page.url();
  await page.keyboard.type("Hi");
  await page.keyboard.type("gi?d/");
  await page.keyboard.press("Enter");
  const pressed = async () => (await inputsOf(run.id, "key")).filter((event) => event.action === "down").map((event) => event.text);
  await expect.poll(pressed).toEqual(["H", "i", "g", "i", "?", "d", "/", "\r"]);
  expect((await inputsOf(run.id, "key")).filter((event) => event.action === "up")).toHaveLength(8);
  expect(page.url()).toBe(here);
  await expect(page.getByTestId("shortcuts-dialog")).toHaveCount(0);
  await expect(page.getByTestId("dispatch-dialog")).toHaveCount(0);
  await expect(keys).toBeFocused();

  // Text an input method gives, whole: inserted, and composed as Telex composes "việt" (shown while it is composed).
  await page.keyboard.insertText("xin chào tiếng Việt");
  const cdp = await page.context().newCDPSession(page);
  for (const text of ["v", "vi", "vie", "viê", "việ", "việt"]) {
    await cdp.send("Input.imeSetComposition", { text, selectionStart: text.length, selectionEnd: text.length });
  }
  await expect(panel.getByTestId("browser-composing")).toHaveText("việt");
  await cdp.send("Input.insertText", { text: "việt" });
  await expect(panel.getByTestId("browser-composing")).toHaveCount(0);
  // A paste goes as text, never as keys.
  await keys.evaluate((element) => {
    const data = new DataTransfer();
    data.setData("text/plain", "Đơn hàng #4471 của Northwind");
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  });
  await expect.poll(() => inputsOf(run.id, "text")).toEqual([
    { type: "text", text: "xin chào tiếng Việt" },
    { type: "text", text: "việt" },
  ]);
  await expect.poll(() => inputsOf(run.id, "paste")).toEqual([{ type: "paste", text: "Đơn hàng #4471 của Northwind" }]);
  expect(await pressed()).toHaveLength(8);

  // The URL box opens an address in the tab watched (https when it names no scheme); another scheme gets a notice.
  const url = panel.getByTestId("browser-url");
  await expect(url).not.toHaveAttribute("readonly");
  await url.fill("northwind.example/invoices");
  await url.press("Enter");
  await expect(url).toHaveValue("https://northwind.example/invoices");
  await expect(keys).toBeFocused();
  await url.fill("file:///etc/hosts");
  await url.press("Enter");
  const message = panel.getByTestId("browser-message");
  await expect(message).toHaveText("The browser says: the URL box opens http and https addresses only");
  await expect(message).toHaveAttribute("data-kind", "notice");
  await expect(url).toHaveValue("https://northwind.example/invoices");
  expect(await inputsOf(run.id, "open")).toEqual([
    { type: "open", url: "northwind.example/invoices" },
    { type: "open", url: "file:///etc/hosts" },
  ]);

  // Back, forward and reload.
  await panel.getByTestId("browser-back").click();
  await expect(url).toHaveValue(NORTHWIND.url);
  await panel.getByTestId("browser-forward").click();
  await expect(url).toHaveValue("https://northwind.example/invoices");
  const screens = (await fakeBrowser(run.id)).screens.length;
  await panel.getByTestId("browser-reload").click();
  await expect.poll(async () => (await fakeBrowser(run.id)).screens.length).toBe(screens + 1);
  await showing(panel, run.id, "northwind");

  // Escape, then Tab, leaves the view; the Tab is the page's own, not the browser's.
  await keys.focus();
  await page.keyboard.press("Escape");
  await page.keyboard.press("Tab");
  await expect(keys).not.toBeFocused();
  expect((await inputsOf(run.id, "key")).filter((event) => event.action === "down").map((event) => event.key)).not.toContain("Tab");

  // Hand back: the worker gives the run to the agent again, and the view is the agent's.
  await panel.getByTestId("browser-handback").click();
  await expect.poll(async () => (await runControl(live, run.id)).handback).toBe(true);
  await reportState(live, run.id, { state: "running", session_id: SESSION });
  await expect(panel).toHaveAttribute("data-mode", "agent");
  await expect(panel.getByTestId("browser-keys")).toHaveCount(0);
  await expect(panel.getByTestId("browser-back")).toBeDisabled();
  await expect(url).toHaveAttribute("readonly", "");
  await expect(panel.getByTestId("browser-takeover")).toBeVisible();

  expect(violations).toEqual([]);
});

test("someone other than the dispatcher sees no Browser tab, and the hub closes their socket with 4403", async ({
  page,
  member,
  admin,
  signInAs,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run } = await browserRun(me);
  const other = newAccount("colleague");
  await admin.grant(project, other.login, "writer", "internal");
  await signOut(page);
  await signInAs(other);
  const violations = await watchCsp(page);

  await open(page, runPath(project, run.id));
  await expect(main(page).getByRole("heading", { level: 1 })).toContainText(`Run #${run.id}`);
  await expect(main(page).getByTestId("trace")).toBeVisible();
  await expect(main(page).getByRole("tab", { name: "Browser", exact: true })).toHaveCount(0);
  await expect(main(page).getByTestId("browser-panel")).toHaveCount(0);

  const refused = await browserCloseCode(page, project, run.id);
  expect(refused).toEqual({ code: 4403, reason: `only ${me.login}, who dispatched run ${run.id}, may view its browser` });
  expect((await fakeBrowser(run.id)).inputs).toEqual([]);
  expect((await runOf(me, project, run.id)).state).toBe("running");
  expect(violations).toEqual([]);
});

test.describe("on a phone", () => {
  const IPHONE_UA =
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
  test.use({ viewport: { width: 375, height: 812 }, userAgent: IPHONE_UA, hasTouch: true, isMobile: true, uiLocale: "vi" });

  test("a tap clicks, a drag scrolls, and Keyboard opens the keys, in Vietnamese", async ({ page, member }) => {
    test.setTimeout(60_000);
    const violations = await watchCsp(page);
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const { project, live, run } = await browserRun(me);

    await open(page, runPath(project, run.id));
    await main(page).getByTestId("run-open-browser").click();
    const panel = main(page).getByTestId("browser-panel");
    await expect(main(page).getByRole("tab", { name: "Trình duyệt", exact: true })).toHaveAttribute("aria-selected", "true");
    await showing(panel, run.id, "northwind");
    await expect(panel.getByTestId("browser-hint")).toHaveText("Agent đang điều khiển trình duyệt này. Tiếp quản để tự dùng.");
    await expect(panel.getByTestId("browser-keyboard")).toHaveCount(0);
    await noSidewaysScroll(page, "the Browser screen at 375 px");

    await panel.getByTestId("browser-takeover").click();
    await expect(panel.getByTestId("browser-hint")).toHaveText("Agent dừng ở cuối lượt, rồi trình duyệt là của bạn.");
    await reportState(live, run.id, { state: "interactive", session_id: SESSION });
    await expect(panel).toHaveAttribute("data-mode", "human");
    await expect(panel.getByTestId("browser-hint")).toHaveText("Bạn đang điều khiển trình duyệt này. Nhấn Esc rồi Tab để rời nó.");
    await expect(panel.getByTestId("browser-handback")).toHaveText("Trả lại cho agent");
    await expectNoSeriousViolations(page, "the Browser screen on a phone while the person drives");
    await noSidewaysScroll(page, "the Browser screen at 375 px while the person drives");

    // A tap is the mouse's move, press and release on the point under the finger.
    const canvas = panel.getByTestId("browser-canvas");
    const box = await pictureBox(canvas);
    const middle = { x: box.left + box.width / 2, y: box.top + box.height / 2 };
    await page.touchscreen.tap(middle.x, middle.y);
    await expect.poll(async () => (await inputsOf(run.id, "mouse")).map((event) => event.action)).toEqual(["move", "down", "up"]);
    for (const event of await inputsOf(run.id, "mouse")) {
      expect(Math.abs((event.x as number) - PAGE_SIZE.width / 2)).toBeLessThan(8);
      expect(Math.abs((event.y as number) - PAGE_SIZE.height / 2)).toBeLessThan(8);
    }

    // A drag up scrolls the page down, by the page's pixels the finger crossed; no click goes with it.
    const cdp = await page.context().newCDPSession(page);
    const touch = (y: number) => [{ x: middle.x, y, id: 1 }];
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: touch(middle.y) });
    for (let step = 1; step <= 6; step++) {
      await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: touch(middle.y - step * 10) });
    }
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    const scrolled = async () => (await inputsOf(run.id, "wheel")).reduce((sum, event) => sum + (event.dy as number), 0);
    await expect.poll(scrolled).toBeGreaterThan(0);
    const expected = (60 * PAGE_SIZE.width) / box.width;
    await expect.poll(scrolled).toBeCloseTo(expected, 0);
    expect((await inputsOf(run.id, "mouse")).map((event) => event.action)).toEqual(["move", "down", "up"]);

    // Keyboard puts the keys in the view, which opens the phone's keyboard; what it types goes as text.
    const keys = panel.getByTestId("browser-keys");
    await keys.blur();
    await panel.getByTestId("browser-keyboard").click();
    await expect(keys).toBeFocused();
    await page.keyboard.insertText("Lumen");
    await expect.poll(() => inputsOf(run.id, "text")).toEqual([{ type: "text", text: "Lumen" }]);

    expect(violations).toEqual([]);
  });
});
