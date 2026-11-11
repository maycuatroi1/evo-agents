import path from "node:path";

import type { Page, Route } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { createAgentByApi } from "./support/agents";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { chainSpec, putSystemByApi, systemOf, systemPath, systemsPath } from "./support/systems";

/**
 * The default system of a project and the Ops tab of a system (docs/hub.md, Agent systems; web/DESIGN.md, Agent
 * systems). An admin sets a system as the default from the list, which then tags it Default; the Ops tab of a system
 * that is not the default says so and offers the same; on the default one an admin pauses and resumes its ops and
 * saves its settings as the system's next revision, with a value out of bounds refused under its field. The cap and
 * the batches are answered by the page's routes, since no ops thread runs in this stack. axe checks the tab in light
 * and dark. With E2E_SCREENSHOT_DIR set it writes review screenshots at 375 px in both themes.
 */
test.skip(isDeployed, "creates agents and systems through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function row(page: Page, slug: string) {
  return main(page).getByTestId("systems-table").locator("tbody tr").filter({ has: page.locator(`a[href$="/systems/${slug}"]`) });
}

async function bothThemes(page: Page, label: string) {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
    await expectNoSeriousViolations(page, `${label} (${scheme})`);
  }
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
}

async function screenshots(page: Page, dir: string, name: string) {
  await page.setViewportSize({ width: 375, height: 812 });
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(dir, `${name}-375-${scheme}.png`), fullPage: true });
  }
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.setViewportSize({ width: 1440, height: 900 });
}

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function batch(id: number, state: string, patch: Record<string, unknown> = {}) {
  return {
    id,
    key: `run_failed:verify:${id}`,
    kind: "run_failed",
    cause: "verify",
    subject: "lumen-shops/checkout",
    state,
    items: 3,
    pending: state === "done" ? 0 : 2,
    opened_at: ago(id * 600_000),
    closes_at: ago(id * 600_000 - 300_000),
    closed_at: null,
    run_id: null,
    recurring: false,
    thread: null,
    ...patch,
  };
}

test("an admin sets the default system, then pauses, resumes and sets its ops from the Ops tab", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const desk = uniqueName("lumen-desk");
  const sales = uniqueName("lumen-sales");
  await createAgentByApi(me, project, { handle: desk, name: "Desk", permissions: {} });
  await createAgentByApi(me, project, { handle: sales, name: "Sales", permissions: {} });
  const ops = uniqueName("northwind-ops");
  const other = uniqueName("northwind-sales");
  await putSystemByApi(me, project, ops, chainSpec("Northwind ops", [{ handle: desk, version: 1 }]));
  await putSystemByApi(me, project, other, chainSpec("Northwind sales", [{ handle: sales, version: 1 }]));

  // The Ops tab of a system that is not the default says so, and Set as default there makes it one.
  await open(page, `${systemPath(project, ops)}?mode=ops`);
  await expect(main(page).getByTestId("system-mode-ops")).toHaveAttribute("aria-current", "page");
  await expect(main(page).getByTestId("ops-not-default")).toBeVisible();
  await expect(main(page).getByTestId("ops-recent")).toHaveCount(0);
  await main(page).getByTestId("ops-not-default").getByTestId("system-set-default").click();
  await expect(main(page).getByTestId("ops-not-default")).toHaveCount(0);
  await expect(main(page).getByTestId("ops-summary")).toBeVisible();
  await expect(main(page).getByTestId("ops-batches-empty")).toBeVisible();

  // The list tags it Default; the other system offers Set as default, and taking it moves the tag.
  await open(page, systemsPath(project));
  await expect(row(page, ops).getByTestId("system-default")).toBeVisible();
  await expect(row(page, ops).getByTestId("system-set-default")).toHaveCount(0);
  await expect(main(page).getByTestId("systems-no-default")).toHaveCount(0);
  await row(page, other).getByTestId("system-set-default").click();
  await expect(row(page, other).getByTestId("system-default")).toBeVisible();
  await expect(row(page, ops).getByTestId("system-default")).toHaveCount(0);
  await row(page, ops).getByTestId("system-set-default").click();
  await expect(row(page, ops).getByTestId("system-default")).toBeVisible();

  // Ops start off. A value out of bounds is refused under its field and focused; turning them on with valid values saves
  // a revision with the spec's ops.
  await open(page, `${systemPath(project, ops)}?mode=ops`);
  await expect(main(page).getByTestId("ops-pause")).toHaveCount(0);
  const before = await systemOf(me, project, ops);
  await main(page).getByTestId("ops-max_items").fill("0");
  await main(page).getByTestId("ops-save").click();
  await expect(main(page).getByTestId("ops-max_items-error")).toBeVisible();
  await expect(main(page).getByTestId("ops-max_items")).toBeFocused();
  await expect(main(page).getByTestId("ops-max_items")).toHaveAttribute("aria-invalid", "true");
  await main(page).getByTestId("ops-enabled").click();
  await main(page).getByTestId("ops-max_items").fill("40");
  await main(page).getByTestId("ops-ops_daily_usd").fill("2.5");
  await main(page).getByTestId("ops-save").click();
  await expect(main(page).getByTestId("system-revision")).toContainText(String(before.revision + 1));
  const after = await systemOf(me, project, ops);
  const spec = after.spec as { ops?: Record<string, unknown>; limits?: Record<string, unknown> };
  expect(spec.ops).toEqual({ enabled: true, debounce_minutes: 5, max_items: 40 });
  expect(spec.limits?.ops_daily_usd).toBe(2.5);
  await expect(main(page).getByTestId("ops-max_items")).toHaveValue("40");

  // Pause with a reason: the banner says so with the reason, and Resume takes it away. Pause only shows while running.
  await main(page).getByTestId("ops-pause").click();
  const dialog = page.getByTestId("ops-pause-dialog");
  await dialog.getByTestId("ops-pause-reason").fill("Checkout deploy freeze");
  await dialog.getByTestId("ops-pause-dialog-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(main(page).getByTestId("ops-paused")).toContainText("Checkout deploy freeze");
  await expect(main(page).getByTestId("ops-pause")).toHaveCount(0);
  await main(page).getByTestId("ops-resume").click();
  await expect(main(page).getByTestId("ops-paused")).toHaveCount(0);
  await expect(main(page).getByTestId("ops-pause")).toBeVisible();

  // At its cap the tab says so; the batches show their state, the recurring one, and the thread of the running one.
  const base = `/v1/projects/${project}/systems/${ops}/ops`;
  await page.route(`**${base}`, async (route: Route) => {
    const response = await route.fetch();
    const body = { ...(await response.json()), spent_usd: 2.61, batches: { open: 1, running: 1 } };
    await route.fulfill({ response, json: body });
  });
  await page.route(`**${base}/batches**`, (route: Route) =>
    route.fulfill({
      json: [
        batch(1, "open"),
        batch(2, "running", { thread: "/chats/7?thread=31", run_id: 812 }),
        batch(3, "done", { kind: "merge_paused", cause: "conflict", subject: "lumen-api", recurring: true }),
        batch(4, "empty", { items: 1, pending: 0 }),
      ],
    }),
  );
  await page.reload();
  await expect(main(page).getByTestId("ops-capped")).toBeVisible();
  await expect(main(page).getByTestId("ops-batches")).toBeVisible();
  await expect(main(page).getByTestId("ops-batch-thread")).toHaveAttribute("href", "/chats/7?thread=31");
  await expect(main(page).getByTestId("ops-batch-recurring")).toBeVisible();
  await bothThemes(page, "ops tab");

  const dir = process.env.E2E_SCREENSHOT_DIR;
  if (dir) {
    await screenshots(page, dir, "system-ops");
    await open(page, systemsPath(project));
    await screenshots(page, dir, "systems-default");
  }
});
