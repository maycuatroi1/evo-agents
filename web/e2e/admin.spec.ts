import type { Browser, Locator, Page } from "@playwright/test";

import type { Locale } from "../src/i18n/locales";
import { call } from "../src/lib/api/client";

import { signIn } from "./support/auth";
import { API_URL, BASE_URL } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, type Account, bearerClient, machineToken, newAccount, uniqueName } from "./support/hub";
import { setUiLocale } from "./support/locale";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * The admin area as a hub admin uses it, against the real API: granting and revoking a role (and what the member
 * sees because of it), revoking a token (which then gets 401), and the audit trail's filters and cursor pages.
 */
test.skip(isDeployed, "changes grants and tokens on the hub");

/**
 * A second browser, signed in as `account`, for what a member sees while the admin works in `page`; it reads the
 * same UI language as the spec's own page.
 */
async function memberBrowser(
  browser: Browser,
  account: Account,
  uiLocale: Locale | null,
): Promise<{ page: Page; close: () => Promise<void> }> {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "vi-VN",
    timezoneId: "Asia/Ho_Chi_Minh",
    reducedMotion: "reduce",
  });
  if (uiLocale) await setUiLocale(context, uiLocale);
  const page = await context.newPage();
  await signIn(page, account);
  return { page, close: () => context.close() };
}

async function adminApi() {
  return bearerClient(await machineToken(ADMIN_ACCOUNT));
}

async function auditIds(table: Locator): Promise<number[]> {
  const ids = await table.locator("[data-audit-id]").evaluateAll((cells) => cells.map((cell) => cell.getAttribute("data-audit-id")));
  return ids.map(Number);
}

test.describe("members and grants", () => {
  test("an admin grants reader to a user, who then sees the project; revoking takes it away", async ({
    page,
    browser,
    admin,
    signInAs,
    uiLocale,
  }) => {
    const project = uniqueName("granted");
    await admin.registerProject(project);
    const account = newAccount("grantee");
    const member = await memberBrowser(browser, account, uiLocale);
    await expect(member.page.getByTestId("state-empty")).toContainText("Bạn chưa được cấp dự án nào");

    await signInAs(ADMIN_ACCOUNT);
    await page.goto("/admin/members");
    await expect(page.getByTestId("admin-nav").getByRole("link", { name: "Thành viên" })).toHaveAttribute("aria-current", "page");
    await page.getByLabel("Tìm theo tên đăng nhập").fill(account.login);
    await expect(page.getByTestId("members-shown")).toContainText("Hiển thị 1 trên");
    await page.getByRole("button", { name: `Cấp quyền cho ${account.login}` }).click();

    const dialog = page.getByRole("dialog", { name: "Cấp quyền theo dự án" });
    await expect(dialog.getByLabel("Tên đăng nhập GitHub")).toHaveValue(account.login);
    await dialog.getByLabel("Dự án").selectOption(project);
    await dialog.getByRole("radio", { name: "Đọc" }).click();
    await dialog.getByLabel("Mức nhãn tối đa").selectOption("internal");
    await dialog.getByRole("button", { name: "Tiếp tục" }).click();

    const confirm = page.getByRole("dialog", { name: "Xác nhận cấp quyền" });
    await expect(confirm.getByTestId("grant-summary")).toContainText(project);
    await expect(confirm.getByTestId("grant-summary")).toContainText("internal");
    await confirm.getByRole("button", { name: "Xác nhận cấp quyền" }).click();
    await expect(confirm).toBeHidden();
    await expect(page.getByTestId("admin-notice-status")).toHaveText(
      `Đã cấp vai trò Đọc, mức internal, cho ${account.login} trong dự án ${project}.`,
    );
    const row = page.getByTestId("members-table").getByRole("row").filter({ hasText: account.login });
    await expect(row).toContainText(project);

    // The member's next page shows the project, and opening it works.
    await member.page.reload();
    await expect(member.page.getByRole("link", { name: `Mở dự án ${project}` })).toBeVisible();
    await member.page.goto(`/p/${project}`);
    await expect(member.page.getByTestId("label-ladder")).toBeVisible();

    // Revoking goes through a confirmation that starts on Cancel.
    await row.getByRole("link", { name: `Quản lý ${account.login}` }).click();
    await expect(page).toHaveURL(new RegExp(`/admin/members/${account.login}$`));
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(account.login);
    await page.getByRole("button", { name: `Thu hồi quyền của ${account.login} trong dự án ${project}` }).click();
    const revoke = page.getByRole("alertdialog", { name: `Thu hồi quyền của ${account.login} trong dự án ${project}?` });
    await expect(revoke.getByRole("button", { name: "Huỷ" })).toBeFocused();
    await revoke.getByRole("button", { name: "Thu hồi quyền" }).click();
    await expect(revoke).toBeHidden();
    await expect(page.getByTestId("admin-notice-status")).toHaveText(`Đã thu hồi quyền của ${account.login} trong dự án ${project}.`);
    await expect(page.getByTestId("member-grants-section")).toContainText("Chưa có quyền ở dự án nào");

    await member.page.goto("/");
    await expect(member.page.getByTestId("state-empty")).toBeVisible();
    await member.page.goto(`/p/${project}`);
    await expect(member.page.getByTestId("state-not-found")).toBeVisible();
    await member.close();

    // Both changes are in the audit trail, filed under the project with its registration.
    await page.goto(`/admin/audit?project=${project}`);
    const rows = page.getByTestId("audit-table").locator("tbody tr");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("grant.delete");
    await expect(rows.nth(0)).toContainText(`${project}/${account.login}`);
    await expect(rows.nth(1)).toContainText(`${project}/${account.login} role=reader max_level=internal`);
    await expect(rows.nth(2)).toContainText("project.register");
    for (const index of [0, 1, 2]) await expect(rows.nth(index)).toContainText(ADMIN_ACCOUNT.login);
  });

  test("a grant the API refuses is explained in the dialog, which then lets the admin fix it", async ({
    page,
    admin,
    signInAs,
  }) => {
    const project = uniqueName("ladder");
    await admin.registerProject(project);
    const account = newAccount("changer");
    await admin.grant(project, account.login, "writer", "customer");
    await signInAs(ADMIN_ACCOUNT);
    await page.goto(`/admin/members/${account.login}`);
    await expect(page.getByTestId("member-grants")).toContainText("customer");
    await page.getByRole("button", { name: `Đổi quyền của ${account.login} trong dự án ${project}` }).click();
    const dialog = page.getByRole("dialog", { name: "Cấp quyền theo dự án" });
    await expect(dialog.getByTestId("grant-existing")).toContainText("đã có đúng vai trò và mức nhãn này");
    await expect(dialog.getByRole("button", { name: "Tiếp tục" })).toBeDisabled();
    await dialog.getByLabel("Mức nhãn tối đa").selectOption("secret");
    await dialog.getByRole("button", { name: "Tiếp tục" }).click();
    const confirm = page.getByRole("dialog", { name: "Xác nhận đổi quyền" });
    await expect(confirm.getByTestId("grant-summary")).toContainText("Ghi, mức customer");

    // Meanwhile the project drops its top level, so the API refuses "secret" with 422.
    await admin.grant(project, account.login, "writer", "internal");
    await admin.registerProject(project, { levels: ["public", "internal", "customer"] });
    await confirm.getByRole("button", { name: "Xác nhận cấp quyền" }).click();
    const error = confirm.getByTestId("admin-dialog-error");
    await expect(error).toContainText("Hub không chấp nhận dữ liệu này.");
    await expect(error).toContainText("max-level must be a level of project");
    await expect(confirm).toBeVisible();

    await confirm.getByRole("button", { name: "Quay lại" }).click();
    const levels = dialog.getByLabel("Mức nhãn tối đa");
    await expect(levels.locator("option")).toHaveText(["public", "internal", "customer"]); // the reloaded ladder
    await levels.selectOption("customer");
    await dialog.getByRole("button", { name: "Tiếp tục" }).click();
    await page.getByRole("dialog", { name: "Xác nhận đổi quyền" }).getByRole("button", { name: "Xác nhận cấp quyền" }).click();
    await expect(page.getByTestId("admin-notice-status")).toHaveText(
      `Đã cấp vai trò Ghi, mức customer, cho ${account.login} trong dự án ${project}.`,
    );
    await expect(page.getByTestId("member-grants")).toContainText("customer");
  });
});

test.describe("tokens", () => {
  test("revoking a token on the tokens page makes that token get 401", async ({ page, signInAs }) => {
    const account = newAccount("laptop");
    const token = await machineToken(account);
    const me = await call(bearerClient(token).GET("/v1/auth/whoami"));
    await signInAs(ADMIN_ACCOUNT);
    await page.goto(`/admin/tokens?login=${account.login}`);
    const row = page.getByTestId("tokens-table").getByRole("row").filter({ hasText: `#${me.token.id}` });
    await expect(row).toContainText("playwright"); // the machine it was issued to
    await expect(row).toContainText("Token máy");
    await expect(row).toContainText("Đang hoạt động");
    await row.getByRole("button", { name: `Thu hồi token ${me.token.id} của ${account.login}` }).click();

    const dialog = page.getByRole("alertdialog", { name: `Thu hồi token ${me.token.id} của ${account.login}?` });
    await expect(dialog).toContainText("Máy playwright sẽ nhận 401");
    await dialog.getByRole("button", { name: "Thu hồi token" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByTestId("admin-notice-status")).toContainText(`Đã thu hồi token ${me.token.id} của ${account.login}.`);
    await expect(page.getByTestId("state-empty")).toBeVisible(); // it was their only live token

    const refused = await fetch(`${API_URL}/v1/auth/whoami`, { headers: { authorization: `Bearer ${token}` } });
    expect(refused.status).toBe(401);
    expect((await refused.json()).message).toContain("evo-agents hub login");

    await page.getByLabel("Trạng thái").selectOption("revoked");
    await page.getByTestId("token-filters-apply").click();
    await expect(page.getByTestId("tokens-table").getByRole("row").filter({ hasText: `#${me.token.id}` })).toContainText("Đã thu hồi");
  });

  test("a token revoked elsewhere meanwhile is reported as a conflict and the list reloads", async ({ page, signInAs }) => {
    const account = newAccount("raced");
    const token = await machineToken(account);
    const me = await call(bearerClient(token).GET("/v1/auth/whoami"));
    await signInAs(ADMIN_ACCOUNT);
    await page.goto(`/admin/tokens?login=${account.login}`);
    await page.getByRole("button", { name: `Thu hồi token ${me.token.id} của ${account.login}` }).click();
    // Another admin gets there first.
    await call((await adminApi()).DELETE("/v1/admin/tokens/{token_id}", { params: { path: { token_id: me.token.id } } }));
    await page.getByRole("alertdialog").getByRole("button", { name: "Thu hồi token" }).click();
    await expect(page.getByTestId("admin-notice-alert")).toContainText("Token này đã bị thu hồi trước đó");
    await expect(page.getByTestId("state-empty")).toBeVisible();
  });
});

test.describe("audit trail", () => {
  test("filtering by actor shows only that actor's rows, and the next page continues the previous one", async ({
    page,
    signInAs,
  }) => {
    const account = newAccount("busy");
    for (let i = 0; i < 27; i += 1) await machineToken(account); // 27 sign-ins, 27 rows of this actor
    const expected = await call(
      (await adminApi()).GET("/v1/admin/audit", { params: { query: { actor: account.login, limit: 200 } } }),
    );
    expect(expected.items).toHaveLength(27);

    await signInAs(ADMIN_ACCOUNT);
    await page.goto("/admin/audit");
    const filters = page.getByRole("search", { name: "Nhật ký audit" });
    await filters.getByLabel("Người thực hiện").fill(account.login);
    await filters.getByLabel("Số dòng mỗi trang").selectOption("25");
    await filters.getByRole("button", { name: "Lọc", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`\\?actor=${account.login}&limit=25$`));

    const table = page.getByTestId("audit-table");
    await expect(table.locator("tbody tr")).toHaveCount(25);
    const actors = await table.locator("[data-actor]").evaluateAll((links) => links.map((link) => link.getAttribute("data-actor")));
    expect(actors).toEqual(Array(25).fill(account.login));
    await expect(page.getByTestId("pager-page")).toHaveText("Trang 1");
    const first = await auditIds(table);

    await page.getByTestId("pager-next").click();
    await expect(page.getByTestId("pager-page")).toHaveText("Trang 2");
    await expect(table.locator("tbody tr")).toHaveCount(2);
    const second = await auditIds(table);
    expect([...first, ...second]).toEqual(expected.items.map((row) => row.id));
    await expect(page.getByTestId("pager-next")).toHaveAttribute("aria-disabled", "true");
    expect(page.url()).toContain("cursor=");

    await page.getByTestId("pager-previous").click();
    await expect(page.getByTestId("pager-page")).toHaveText("Trang 1");
    await expect.poll(() => auditIds(table)).toEqual(first);

    // A page opened from its link has no way back but to the first page.
    await page.getByTestId("pager-next").click();
    await expect(page.getByTestId("pager-page")).toHaveText("Trang 2");
    await page.reload();
    await expect(table.locator("tbody tr")).toHaveCount(2);
    await page.getByTestId("pager-first").click();
    await expect(page.getByTestId("pager-page")).toHaveText("Trang 1");
  });

  test("an empty filter result says so, and a bad date range is refused before asking the API", async ({ page, signInAs }) => {
    await signInAs(ADMIN_ACCOUNT);
    await page.goto(`/admin/audit?actor=${newAccount("ghost").login}`);
    await expect(page.getByTestId("state-empty")).toContainText("Không có dòng audit nào khớp bộ lọc");
    const filters = page.getByRole("search", { name: "Nhật ký audit" });
    await filters.getByLabel("Từ ngày").fill("2026-10-04");
    await filters.getByLabel("Đến ngày").fill("2026-10-01");
    await filters.getByRole("button", { name: "Lọc", exact: true }).click();
    await expect(filters.getByText("Ngày kết thúc phải bằng hoặc sau ngày bắt đầu.")).toBeVisible();
    await expect(filters.getByLabel("Đến ngày")).toBeFocused();
    expect(page.url()).not.toContain("from=");
  });
});

const ADMIN_PATHS = ["/admin", "/admin/members", `/admin/members/${ADMIN_ACCOUNT.login}`, "/admin/tokens", "/admin/audit"];

test("admin pages hydrate without a console error or a page error", async ({ page, signInAs }) => {
  await signInAs(ADMIN_ACCOUNT);
  const problems: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`page: ${error.message}`));
  for (const width of [1440, 375]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ADMIN_PATHS) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
    }
  }
  expect(problems).toEqual([]);
});

test("admin pages never scroll sideways, from a 375 px phone to a 1440 px desktop", async ({ page, signInAs }) => {
  await signInAs(ADMIN_ACCOUNT);
  await machineToken(ADMIN_ACCOUNT); // a wide row in every table: a machine token next to the web sessions
  for (const width of [375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ADMIN_PATHS) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `${path} at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
    }
  }
  // At 375 px every admin tab is in view, without scrolling the tab list.
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/admin/audit");
  for (const name of ["Tổng quan", "Thành viên", "Token", "Audit"])
    await expect(page.getByTestId("admin-nav").getByRole("link", { name, exact: true })).toBeInViewport({ ratio: 1 });
});
