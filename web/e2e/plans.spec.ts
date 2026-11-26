import type { Page, Request } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, test } from "./support/fixtures";
import {
  ACTIVE_DONE,
  ACTIVE_PLAN,
  ACTIVE_STEPS,
  CHANGED_LINES,
  COMPLETED_PLAN,
  COMPLETED_STEPS,
  DONE_AT,
  EVIDENCE,
  EVIDENCE_STEP,
  open,
  seedPlans,
} from "./support/plans";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * The plan pages against the real API (step 26 of the agent-hub plan): the list shows each plan's current step and
 * its steps in order, a step shows its evidence verbatim, the diff between two revisions shows exactly the lines that changed,
 * and no page asks the API to change a plan: until a writer opens a dialog (Run plan, Run this step), every request
 * the pages make is a GET. e2e/plan-runs.spec.ts covers what those dialogs send.
 */
test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds plans through the local stack");

const WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Every request the page makes that would change a plan: a write to the plans API, or any write to a plans page. */
function watchPlanWrites(page: Page): Request[] {
  const writes: Request[] = [];
  page.on("request", (request) => {
    const { pathname } = new URL(request.url());
    if (WRITES.has(request.method()) && /\/plans(\/|$)/.test(pathname)) writes.push(request);
  });
  return writes;
}

async function seeded(member: (grants?: { role: "reader" | "writer" | "admin"; maxLevel: string }[]) => Promise<{ login: string; id: number; projects: string[] }>) {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const { revisions } = await seedPlans(me, project);
  return { me, project, revisions };
}

test.describe("plans", () => {
  test("one list of plans with their steps at a glance", async ({ page, member }) => {
    const writes = watchPlanWrites(page);
    const { project } = await seeded(member);

    await open(page, `/p/${project}`);
    await page.getByRole("navigation", { name: "Điều hướng chính" }).getByRole("link", { name: "Plans" }).click();
    await expect(page).toHaveURL(`/p/${project}/plans`);
    await expect(page.getByRole("heading", { level: 1, name: "Plans" })).toBeVisible();
    await expect(page.getByTestId("plans-cli-note")).toHaveText("Sửa từ CLI");
    await expect(page.getByTestId("plans-read-only")).toHaveCount(0);

    // Open by default: the active plan, named by its goal, with its slug, current step and one mark per step.
    const list = page.getByTestId("plans-list");
    await expect(list).toHaveAttribute("data-view", "open");
    await expect(page.getByTestId("plans-view-open")).toHaveAttribute("aria-pressed", "true");
    await expect(list.getByTestId("plan-row")).toHaveCount(1);
    const active = list.locator(`[data-plan-id="${ACTIVE_PLAN}"]`);
    await expect(active.getByTestId("plan-link")).toHaveText(/^Knowledge graph được dùng hằng ngày/);
    await expect(active.getByTestId("plan-slug")).toHaveText(ACTIVE_PLAN);
    await expect(active.getByTestId("plan-current-step")).toHaveText(new RegExp(`^Bước ${ACTIVE_DONE + 1} trên ${ACTIVE_STEPS}: `));
    await expect(active.getByTestId("plan-current-step")).toHaveAttribute("data-status", "in_progress");
    const marks = active.getByTestId("step-strip").locator("[data-status]");
    await expect(marks).toHaveCount(ACTIVE_STEPS);
    expect(await marks.evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-status")))).toEqual([
      ...Array<string>(ACTIVE_DONE).fill("done"),
      "in_progress",
      "blocked",
      ...Array<string>(ACTIVE_STEPS - ACTIVE_DONE - 2).fill("pending"),
    ]);
    await expect(active.getByTestId("plan-steps-count")).toContainText(`${ACTIVE_DONE}/${ACTIVE_STEPS} bước xong`);
    await expect(active).toHaveAttribute("data-finished", "false");
    await expect(active.getByRole("button")).toHaveCount(0);

    // Completed: both completed plans, last changed first, faint since every step is done.
    await page.getByTestId("plans-view-completed").click();
    await expect(page).toHaveURL(new RegExp(`/plans\\?view=completed$`));
    await expect(list.getByTestId("plan-row")).toHaveCount(2);
    const completed = list.locator(`[data-plan-id="${COMPLETED_PLAN}"]`);
    await expect(completed).toHaveAttribute("data-finished", "true");
    await expect(completed.getByTestId("plan-current-step")).toHaveText(`Xong cả ${COMPLETED_STEPS} bước`);
    await expect(completed.getByTestId("plan-steps-count")).toContainText(`${COMPLETED_STEPS}/${COMPLETED_STEPS} bước xong`);

    // The search reads the id too, and `/` reaches it.
    const search = page.locator("#main").getByTestId("plans-search");
    await expect(search).toHaveAttribute("aria-keyshortcuts", "/");
    await page.keyboard.press("/");
    await expect(search).toBeFocused();
    await page.keyboard.type("prototype");
    await expect(list.getByTestId("plan-row")).toHaveCount(1);
    await expect(completed).toBeVisible();
    await expect(page.getByText("Hiện 1 trên 2 plan")).toBeVisible();

    // A plan page also counts the done steps of the fixture plan correctly.
    await completed.getByTestId("plan-link").click();
    await expect(page.getByTestId("plan-progress")).toContainText(`${COMPLETED_STEPS} trên ${COMPLETED_STEPS} bước đã xong`);
    await expect(page.getByTestId("count-done")).toHaveText(String(COMPLETED_STEPS));
    await expect(page.getByTestId("board-column-done").getByTestId("column-count")).toHaveText(String(COMPLETED_STEPS));
    expect(writes.map((r) => `${r.method()} ${r.url()}`)).toEqual([]);
  });

  test("the list passes axe at 1440 and 375 px in light and dark, and never scrolls sideways from 375 to 1440 px", async ({ page, member }) => {
    test.setTimeout(90_000);
    const { project } = await seeded(member);
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const width of [375, 768, 1024, 1440]) {
        await page.setViewportSize({ width, height: width === 375 ? 812 : 900 });
        // Grouped by repo, so the group headings are scanned too.
        await open(page, `/p/${project}/plans?group=repo`);
        await expect(page.getByTestId("plans-list").getByTestId("plan-row").first()).toBeVisible();
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect.soft(overflow, `the plans list at ${width} px in ${scheme} scrolls sideways`).toBeLessThanOrEqual(0);
        if (width === 375 || width === 1440) await expectNoSeriousViolations(page, `the plans list in ${scheme} at ${width} px`);
      }
    }
  });

  test("the board groups steps by status, and a step shows its evidence verbatim", async ({ page, member }) => {
    const writes = watchPlanWrites(page);
    const { project } = await seeded(member);
    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);

    await expect(page.getByRole("heading", { level: 1 })).toHaveText(ACTIVE_PLAN);
    await expect(page.getByTestId("plan-revision")).toHaveText("Revision 3");
    await expect(page.getByTestId("plan-progress")).toContainText(`${ACTIVE_DONE} trên ${ACTIVE_STEPS} bước đã xong (50%)`);
    for (const [group, count] of [
      ["pending", 8],
      ["in_progress", 1],
      ["blocked", 1],
      ["done", ACTIVE_DONE],
    ] as const) {
      await expect(page.getByTestId(`count-${group}`)).toHaveText(String(count));
      await expect(page.getByTestId(`board-column-${group}`).getByTestId("column-count")).toHaveText(String(count));
    }
    const blocked = page.getByTestId("board-column-blocked");
    await expect(blocked.getByRole("heading", { name: /Bị chặn/ })).toBeVisible();
    const card = blocked.getByTestId("step-card");
    await expect(card).toHaveAttribute("data-step", "12");
    await expect(card).toContainText("evo-agents");
    await expect(card.locator("[data-blocking=true]")).toHaveText("Chặn tiến độ");
    await expect(card).toContainText("Sau");

    // A long done column folds after 8 cards and opens on request.
    const done = page.getByTestId("board-column-done");
    await expect(done.getByTestId("step-card")).toHaveCount(8);
    await done.getByRole("button", { name: `Hiện cả ${ACTIVE_DONE} bước` }).click();
    await expect(done.getByTestId("step-card")).toHaveCount(ACTIVE_DONE);

    // The list view shows the same steps in plan order, with depends_on.
    await page.getByTestId("steps-view-list").click();
    await expect(page.getByTestId("steps-view-list")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("steps-table").getByRole("row")).toHaveCount(ACTIVE_STEPS + 1);

    await page.getByTestId("steps-view-board").click();
    await done.getByRole("button", { name: `Hiện cả ${ACTIVE_DONE} bước` }).click();
    await page
      .getByTestId("board-column-done")
      .locator(`[data-step="${EVIDENCE_STEP}"]`)
      .getByRole("link", { name: new RegExp(`^Mở bước ${EVIDENCE_STEP}:`) })
      .click();
    await expect(page).toHaveURL(`/p/${project}/plans/${ACTIVE_PLAN}/steps/${EVIDENCE_STEP}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Hook PostToolUse cộng dồn nhãn phiên");
    const evidence = page.getByTestId("step-evidence");
    await expect(evidence).toBeVisible();
    expect(await evidence.textContent()).toBe(EVIDENCE);
    await expect(page.getByTestId("step-done-at")).toHaveAttribute("datetime", DONE_AT);
    const facts = page.getByTestId("step-facts");
    await expect(facts).toContainText("Xong");
    await expect(facts.getByRole("link", { name: /^Bước 1:/ })).toHaveAttribute("href", `/p/${project}/plans/${ACTIVE_PLAN}/steps/1`);
    await expect(page.getByTestId("step-what")).toContainText("PostToolUse");
    await expect(page.getByTestId("step-verify")).toContainText("pytest tests/kg/test_hooks.py -k post_tool -q");

    // Previous and next step, and a step that has no evidence yet.
    await page.getByRole("navigation", { name: "Bước trước và bước sau" }).getByRole("link", { name: /Bước sau/ }).click();
    await expect(page).toHaveURL(new RegExp(`/steps/11$`));
    await expect(page.getByText("Chưa có bằng chứng: bước chưa xong.")).toBeVisible();
    await expect(page.getByTestId("step-facts")).toContainText("Đang làm");

    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}/steps/99`);
    await expect(page.getByTestId("state-not-found")).toContainText(`Plan ${ACTIVE_PLAN} không có bước 99`);
    expect(writes.map((r) => `${r.method()} ${r.url()}`)).toEqual([]);
  });

  test("the diff between two revisions shows exactly the lines that changed", async ({ page, member }) => {
    const writes = watchPlanWrites(page);
    const { project, revisions } = await seeded(member);
    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);
    await page.getByTestId("plan-tab-revisions").click();
    await expect(page).toHaveURL(`/p/${project}/plans/${ACTIVE_PLAN}/revisions`);

    // The history: newest first, with actor, time and summary.
    const items = page.getByTestId("revision-item");
    await expect(items).toHaveCount(revisions);
    await expect(items.first()).toHaveAttribute("data-revision", "3");
    await expect(items.first().getByTestId("revision-summary")).toHaveText("step 11: status blocked -> in_progress");
    await expect(items.nth(1).getByTestId("revision-summary")).toHaveText("step 10: status in_progress -> done; set done_at, evidence");
    await expect(items.last().getByTestId("revision-summary")).toHaveText("created");
    await expect(items.first()).toContainText("e2e-member-");
    // By default the latest revision is compared with the one before it.
    await expect(page.getByTestId("diff-title")).toHaveText("Thay đổi từ revision 2 tới revision 3");

    // Pick revisions 1 and 2 in the form: a GET navigation, never a write.
    const form = page.getByTestId("compare-form");
    await form.getByLabel("Từ revision").selectOption("1");
    await form.getByLabel("Tới revision").selectOption("2");
    await form.getByTestId("compare-submit").click();
    await expect(page).toHaveURL(`/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=1&to=2`);
    await expect(page.getByTestId("diff-title")).toHaveText("Thay đổi từ revision 1 tới revision 2");

    const diff = page.getByTestId("plan-diff");
    await expect(diff.getByTestId("diff-hunk")).toHaveCount(1);
    const text = (kind: string) =>
      diff.locator(`[data-testid=diff-line][data-kind=${kind}]`).getByTestId("diff-text").allTextContents();
    expect(await text("removed")).toEqual(CHANGED_LINES.removed);
    expect(await text("added")).toEqual(CHANGED_LINES.added);
    expect(await text("context")).toHaveLength(6);
    await expect(page.getByTestId("diff-stats")).toContainText(`+${CHANGED_LINES.added.length} dòng thêm`);
    await expect(page.getByTestId("diff-stats")).toContainText(`-${CHANGED_LINES.removed.length} dòng xoá`);
    // Each changed line is named in words, not only by colour and sign.
    await expect(diff.locator("[data-kind=removed]").first()).toContainText("Xoá:");
    await expect(diff.locator("[data-kind=added]").first()).toContainText("Thêm:");

    // Side by side puts the removed line beside the line that replaced it.
    await page.getByTestId("diff-mode-split").click();
    const row = diff.getByRole("row").filter({ hasText: "status: in_progress" });
    await expect(row).toContainText("status: done");

    // The link of a history entry compares it with the revision before it.
    await items.nth(0).getByTestId("revision-compare").click();
    await expect(page).toHaveURL(`/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=2&to=3`);
    await expect(items.first()).toHaveAttribute("aria-current", "true");
    await expect(page.getByTestId("plan-diff").locator("[data-kind=removed]")).toContainText(["status: blocked"]);

    // The same revision on both sides says so instead of an empty diff.
    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=2&to=2`);
    await expect(page.getByTestId("diff-same")).toBeVisible();
    expect(writes.map((r) => `${r.method()} ${r.url()}`)).toEqual([]);
  });

  test("plan pages send only GETs while no dialog is open, and the plan is unchanged after visiting all of them", async ({ page, member }) => {
    const writes = watchPlanWrites(page);
    const methods = new Map<string, number>();
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/v1/projects/")) {
        methods.set(request.method(), (methods.get(request.method()) ?? 0) + 1);
      }
    });
    const { project, revisions } = await seeded(member);
    const base = `/p/${project}/plans`;
    // The writer is offered Chạy plan on an active plan's page, not on a completed one's; neither sends anything.
    await open(page, `${base}/${ACTIVE_PLAN}`);
    await expect(page.locator("#main").getByRole("button", { name: "Chạy plan" })).toBeVisible();
    await open(page, `${base}/${COMPLETED_PLAN}`);
    await expect(page.locator("#main").getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.locator("#main").getByRole("button", { name: "Chạy plan" })).toHaveCount(0);
    for (const path of [
      base,
      `${base}/${COMPLETED_PLAN}`,
      `${base}/${COMPLETED_PLAN}/revisions`,
      `${base}/${ACTIVE_PLAN}`,
      `${base}/${ACTIVE_PLAN}/steps/${EVIDENCE_STEP}`,
      `${base}/${ACTIVE_PLAN}/revisions`,
      `${base}/${ACTIVE_PLAN}/revisions?from=1&to=3`,
    ]) {
      await open(page, path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      // The list says in one line that plans are edited from the CLI; a plan's pages carry the read-only banner.
      if (path === base) await expect(page.getByTestId("plans-cli-note")).toBeVisible();
      else await expect(page.getByTestId("plans-read-only")).toContainText("evo harness step");
      // No control on the page edits, deletes or completes a plan; the writer's Chạy plan (Run plan) and Chạy bước này
      // (Run this step) only open a dialog, and the banner's Sao chép (Copy) only copies the command.
      await expect(page.getByRole("button", { name: /^(Sửa|Xoá|Lưu|Hoàn tất|Đánh dấu)/ })).toHaveCount(0);
    }

    // Client-side moves too: tabs, a step, back, the board's list view, a search.
    await page.getByTestId("plan-tab-steps").click();
    await page.getByTestId("steps-view-list").click();
    await page.getByTestId("steps-search").fill("hook");
    await page.getByTestId("plan-tab-revisions").click();
    await page.getByTestId("diff-mode-split").click();
    await page.getByRole("navigation", { name: "Vị trí hiện tại" }).getByRole("link", { name: "Plans", exact: true }).click();
    await expect(page.getByTestId("plans-list")).toBeVisible();

    expect(writes.map((r) => `${r.method()} ${r.url()}`)).toEqual([]);
    expect([...methods.keys()].filter((method) => method !== "GET")).toEqual([]);
    const api = await page.request.get(`/v1/projects/${project}/plans/${ACTIVE_PLAN}/revisions`);
    expect(api.status()).toBe(200);
    expect(((await api.json()) as unknown[]).length).toBe(revisions);
  });

  test("the plan pages work with the keyboard alone", async ({ page, member }) => {
    const { project } = await seeded(member);
    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);
    const link = page.getByTestId("board-column-in_progress").getByRole("link", { name: /^Mở bước 11:/ });
    await link.focus();
    await expect(link).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/steps/11$`));
    await expect(page.getByRole("heading", { level: 1 })).toContainText("11");

    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=1&to=2`);
    const split = page.getByTestId("diff-mode-split");
    // Pressing again keeps the side-by-side view, so retrying until the page has hydrated is safe.
    await expect(async () => {
      await split.focus();
      await page.keyboard.press("Space");
      await expect(split).toHaveAttribute("aria-pressed", "true", { timeout: 1_000 });
    }).toPass();
    const form = page.getByTestId("compare-form");
    await form.getByLabel("Từ revision").focus();
    await page.keyboard.press("Tab");
    await expect(form.getByLabel("Tới revision")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(form.getByTestId("compare-submit")).toBeFocused();
  });

  test("the plan pages fit a 375 px screen without scrolling sideways", async ({ page, member }) => {
    const { project } = await seeded(member);
    await page.setViewportSize({ width: 375, height: 812 });
    for (const path of [
      `/p/${project}/plans`,
      `/p/${project}/plans/${ACTIVE_PLAN}`,
      `/p/${project}/plans/${ACTIVE_PLAN}/steps/${EVIDENCE_STEP}`,
      `/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=1&to=2`,
    ]) {
      await open(page, path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, path).toBeLessThanOrEqual(0);
    }
    // On a phone the diff is always unified: the side-by-side toggle is not offered.
    await expect(page.getByTestId("diff-mode-split")).toBeHidden();
    await expect(page.getByTestId("diff-line").first()).toBeVisible();
  });
});
