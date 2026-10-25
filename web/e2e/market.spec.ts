import path from "node:path";

import type { Page, TestInfo } from "@playwright/test";

import { call } from "../src/lib/api/client";

import { expectNoSeriousViolations } from "./support/a11y";
import { signOut } from "./support/auth";
import { STACK_URL } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT } from "./support/hub";
import { expandInbox } from "./support/inbox";
import { apiOf } from "./support/memories";
import { toast } from "./support/toast";

/**
 * Criteria a5 and a6 of plan plugin-market in the browser, against the real API (docs/market.md): a hub admin adds a
 * market, a GitHub repo with a plugin-market-v1 index; every member browses its plugins with search and industry and
 * category filters and reads a plugin's publisher, signing identity, permissions, version, price and status, a core
 * plugin of the image reading as in the image; a hub admin installs a free one from its GitHub release, asked first
 * to trust its new publisher, and upgrades it on a new release; a project's admin asks the hub admins for one, which
 * lands in their Inbox. GitHub is the stack's fake (hub_stack.py, market_seed.py): each test seeds a market and plugin
 * of its own, whose releases an Ed25519 key of the Python tests signs.
 *
 * The screens are checked with axe in light and dark, at 1440 and 375 px, and written to E2E_SCREENSHOT_DIR when set.
 */
test.skip(isDeployed, "seeds markets and releases on the local stack's fake GitHub");

/** How the hub names lumen's Ed25519 signer: the id of its key. */
const LUMEN_IDENTITY = /ed25519:[0-9a-f]{16}/;

type Seeded = { market: string; repo: string; plugin: string; name: string; signer: string };

async function stack<T>(route: string, body: unknown): Promise<T> {
  const response = await fetch(`${STACK_URL}${route}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`hub_stack ${route}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** A market repo of its own on the fake GitHub, with a free plugin of lumen's released at 1.0.0. */
async function seedMarket(): Promise<Seeded> {
  const tag = Math.random().toString(16).slice(2, 10);
  const seeded = { market: `northwind/market-${tag}`, repo: `lumen-labs/notes-${tag}`, plugin: `lumen-notes-${tag}`, name: `Lumen notes ${tag}` };
  const { signer } = await stack<{ signer: string }>("/market/seed", { ...seeded, version: "1.0.0" });
  return { ...seeded, signer };
}

/** Add the seeded market through the API, as `evo-agents hub market add` does; its source id. */
async function addSource(seeded: Seeded): Promise<number> {
  const api = await apiOf(ADMIN_ACCOUNT);
  const source = await call(api.POST("/v1/market/sources", { body: { repo: seeded.market, ref: "main", path: "market.json", token: null } }));
  expect(source.error).toBeNull();
  return source.id;
}

function main(page: Page) {
  return page.locator("#main");
}

async function shoot(page: Page, info: TestInfo, name: string) {
  const dir = process.env.E2E_SCREENSHOT_DIR;
  const file = dir ? path.join(dir, `market-${name}.png`) : info.outputPath(`market-${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
}

async function noSidewaysScroll(page: Page, label: string) {
  const widths = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  expect(widths[0], `${label} scrolls sideways`).toBeLessThanOrEqual(widths[1]);
}

test("a hub admin adds a market, members browse it, and the admin installs, upgrades and stops trusting", async ({ page, signInAs }) => {
  test.setTimeout(120_000);
  const seeded = await seedMarket();
  await signInAs(ADMIN_ACCOUNT);

  // Add the market from the admin area, by its github.com address.
  await page.goto("/admin/market");
  await expect(page.getByTestId("admin-nav").getByRole("link", { name: "Market" })).toHaveAttribute("aria-current", "page");
  await main(page).getByTestId("market-source-add").click();
  const dialog = page.getByTestId("market-source-dialog");
  await dialog.getByTestId("market-source-repo").fill(`https://github.com/${seeded.market}.git`);
  await dialog.getByTestId("market-source-save").click();
  await expect(toast(page, `Added ${seeded.market}`)).toBeVisible();
  const sourceRow = main(page).getByTestId("market-sources").locator("tr", { has: page.locator(`[data-source-repo="${seeded.market}"]`) });
  await expect(sourceRow).toContainText("Northwind market");

  // Browse: search, then the industry and category filters, all kept in the address.
  await page.goto("/market");
  await expect(page.getByTestId("nav-market")).toHaveAttribute("aria-current", "page");
  const table = main(page).getByTestId("market-table");
  const free = table.locator(`[data-market-link="${seeded.plugin}"]`);
  const paid = table.locator(`[data-market-link="${seeded.plugin}-ledger"]`);
  await main(page).getByTestId("market-search").fill(seeded.plugin);
  await expect(free).toBeVisible();
  await expect(paid).toBeVisible();
  await expect(table.locator('[data-market-link="notes"]')).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`q=${seeded.plugin}`));
  await main(page).getByTestId("market-industry").selectOption("education");
  await expect(free).toBeVisible();
  await expect(paid).toHaveCount(0);
  await main(page).getByTestId("market-category").selectOption("sales");
  await expect(main(page).getByText("No plugin matches these filters")).toBeVisible();
  await main(page).getByTestId("market-category").selectOption("");
  const freeRow = table.locator("tr", { has: page.locator(`[data-market-link="${seeded.plugin}"]`) });
  await expect(freeRow.getByTestId("market-status")).toHaveAttribute("data-status", "not_installed");
  await expect(freeRow.getByTestId("market-price")).toHaveText("Free");

  // The plugin's page: its publisher and signer, permissions, version, price and status.
  await free.click();
  await expect(page).toHaveURL(new RegExp(`/market/\\d+/${seeded.plugin}$`));
  const source = Number(new URL(page.url()).pathname.split("/")[2]);
  const detail = main(page).getByTestId("market-plugin");
  await expect(detail.getByRole("heading", { level: 1 })).toHaveText(seeded.name);
  await expect(detail.getByTestId("market-status")).toHaveAttribute("data-status", "not_installed");
  await expect(detail.getByTestId("market-price")).toHaveText("Free");
  await expect(detail.getByTestId("market-publisher")).toContainText("Lumen Labs");
  await expect(detail.getByTestId("market-identity")).toContainText(LUMEN_IDENTITY);
  await expect(detail.getByTestId("market-trusted")).toContainText("Not trusted yet");
  await expect(detail.getByTestId("market-latest")).toContainText("1.0.0");
  await expect(detail.getByTestId("market-installed")).toContainText("None");
  await expect(detail.locator('[data-permission="runs:read"]')).toBeVisible();

  // Install: the hub asks before trusting lumen's signer, then installs from the release.
  await detail.getByTestId("market-install").click();
  const trust = page.getByTestId("market-trust-dialog");
  await expect(trust).toContainText("Trust Lumen Labs?");
  await expect(trust.getByTestId("market-trust-identity")).toHaveText(LUMEN_IDENTITY);
  await expectNoSeriousViolations(page, "the trust dialog");
  await trust.getByTestId("market-trust-confirm").click();
  await expect(toast(page, `Installed ${seeded.name} 1.0.0`)).toBeVisible();
  await expect(trust).toBeHidden();
  await expect(detail.getByTestId("market-status")).toHaveAttribute("data-status", "installed");
  await expect(detail.getByTestId("market-installed")).toContainText("1.0.0");
  await expect(detail.getByTestId("market-trusted")).toContainText("Trusted");

  // A new release: a refresh reads it, and the upgrade installs it without asking again.
  await stack("/market/release", { repo: seeded.repo, plugin: seeded.plugin, name: seeded.name, version: "1.1.0" });
  await page.goto("/admin/market");
  await sourceRow.getByTestId("market-source-refresh").click();
  await expect(toast(page, `Read ${seeded.market}: 3 plugins`)).toBeVisible();
  await page.goto(`/market/${source}/${seeded.plugin}`);
  await expect(detail.getByTestId("market-status")).toHaveAttribute("data-status", "upgrade");
  await detail.getByTestId("market-install").click();
  await expect(toast(page, `Upgraded ${seeded.name} to 1.1.0`)).toBeVisible();
  await expect(detail.getByTestId("market-status")).toHaveAttribute("data-status", "installed");
  await expect(detail.getByTestId("market-installed")).toContainText("1.1.0");

  // A core plugin of the image reads as in it, and installs from nowhere; a paid one needs a license.
  await page.goto(`/market/${source}/notes`);
  await expect(detail.getByTestId("market-status")).toHaveAttribute("data-status", "in_image");
  await expect(detail.getByTestId("market-reason")).toBeVisible();
  await expect(detail.getByTestId("market-install")).toHaveCount(0);
  await page.goto(`/market/${source}/${seeded.plugin}-ledger`);
  await expect(detail.getByTestId("market-status")).toHaveAttribute("data-status", "needs_license");
  await expect(detail.getByTestId("market-price")).toContainText("per hub a month");
  await expect(detail.getByTestId("market-license")).toBeVisible();
  await expect(detail.getByTestId("market-install")).toHaveCount(0);
  // A hub admin buys it for some months: the dialog totals the order and says it trusts the new publisher.
  await detail.getByTestId("market-buy").click();
  const buying = page.getByTestId("market-buy-dialog");
  await buying.getByTestId("market-buy-months").fill("3");
  await expect(buying.getByTestId("market-buy-total")).toContainText("148.50");
  await expect(buying.getByTestId("market-buy-trust")).toContainText("northwind");
  await expect(buying.getByTestId("market-buy-confirm")).toBeDisabled();
  await page.keyboard.press("Escape");

  // Stop trusting lumen's signer, then remove the market. The hub has bought no plugin yet.
  await page.goto("/admin/market");
  await expect(main(page).getByTestId("market-licenses")).toContainText("The hub has bought no plugin.");
  const trusted = main(page).getByTestId("market-trusted").locator('[data-trusted-publisher="lumen"]');
  await expect(trusted).toContainText(LUMEN_IDENTITY);
  await trusted.getByTestId("market-trust-remove").click();
  await page.getByTestId("market-remove-confirm").click();
  await expect(toast(page, "Stopped trusting lumen")).toBeVisible();
  await expect(trusted).toHaveCount(0);
  await sourceRow.getByTestId("market-source-remove").click();
  await page.getByTestId("market-remove-confirm").click();
  await expect(toast(page, `Removed ${seeded.market}`)).toBeVisible();
  await expect(sourceRow).toHaveCount(0);
});

test.describe("a project's admin in Vietnamese", () => {
  test.use({ uiLocale: "vi" });

  test("asks the hub admins for a plugin, and they find it in their Inbox", async ({ page, member, signInAs }) => {
    const seeded = await seedMarket();
    const source = await addSource(seeded);
    const asker = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = asker.projects[0]!;

    await page.goto(`/market/${source}/${seeded.plugin}`);
    const detail = main(page).getByTestId("market-plugin");
    await expect(detail.getByTestId("market-status")).toHaveText("Chưa cài");
    await expect(detail.getByTestId("market-install")).toHaveCount(0);
    const request = detail.getByTestId("market-request");
    await expect(request.getByTestId("market-request-project")).toHaveValue(project);
    await request.getByTestId("market-request-send").click();
    await expect(toast(page, `Đã đề nghị quản trị viên hub cài cho ${project}`)).toBeVisible();
    await request.getByTestId("market-request-send").click();
    await expect(toast(page, `Đã có đề nghị cho ${project} rồi`)).toBeVisible();

    await signOut(page);
    await signInAs(ADMIN_ACCOUNT);
    await page.goto("/inbox");
    await expandInbox(main(page));
    await expect(main(page).getByText(`${asker.login} asks for ${seeded.name} in ${project}`)).toBeVisible();
  });
});

for (const scheme of ["light", "dark"] as const) {
  test(`the market's screens in ${scheme}, at 1440 and 375 px`, async ({ page, signInAs }, info) => {
    test.setTimeout(120_000);
    await page.emulateMedia({ colorScheme: scheme });
    const seeded = await seedMarket();
    const source = await addSource(seeded);
    await signInAs(ADMIN_ACCOUNT);
    for (const [width, suffix] of [
      [1440, ""],
      [375, "-mobile"],
    ] as const) {
      await page.setViewportSize({ width, height: width === 375 ? 812 : 900 });
      await page.goto(`/market?q=${seeded.plugin}`);
      await expect(main(page).getByTestId("market-table")).toContainText(seeded.name);
      await noSidewaysScroll(page, `the market at ${width} px`);
      await expectNoSeriousViolations(page, `the market in ${scheme} at ${width} px`);
      await shoot(page, info, `list${suffix}-${scheme}`);

      await page.goto(`/market/${source}/${seeded.plugin}`);
      await expect(main(page).getByTestId("market-permissions")).toBeVisible();
      await noSidewaysScroll(page, `a market plugin at ${width} px`);
      await expectNoSeriousViolations(page, `a market plugin in ${scheme} at ${width} px`);
      await shoot(page, info, `plugin${suffix}-${scheme}`);

      await page.goto(`/market/${source}/${seeded.plugin}-ledger`);
      await expect(main(page).getByTestId("market-license")).toBeVisible();
      await expectNoSeriousViolations(page, `a paid market plugin in ${scheme} at ${width} px`);
      await shoot(page, info, `paid${suffix}-${scheme}`);
      await main(page).getByTestId("market-buy").click();
      await expect(page.getByTestId("market-buy-dialog")).toBeVisible();
      await expectNoSeriousViolations(page, `the buy dialog in ${scheme} at ${width} px`);
      await shoot(page, info, `buy${suffix}-${scheme}`);
      await page.keyboard.press("Escape");

      await page.goto("/admin/market");
      await expect(main(page).locator(`[data-source-repo="${seeded.market}"]`).first()).toBeVisible();
      await noSidewaysScroll(page, `the admin's markets at ${width} px`);
      await expectNoSeriousViolations(page, `the admin's markets in ${scheme} at ${width} px`);
      await shoot(page, info, `admin${suffix}-${scheme}`);

      await main(page).getByTestId("market-source-add").click();
      await expect(page.getByTestId("market-source-dialog")).toBeVisible();
      await expectNoSeriousViolations(page, `the add-market dialog in ${scheme} at ${width} px`);
      await shoot(page, info, `add${suffix}-${scheme}`);
      await page.keyboard.press("Escape");
    }
  });
}
