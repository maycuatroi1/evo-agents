import type { Page } from "@playwright/test";

import { cspSources } from "./support/csp";
import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { planRunUnderway, seedPlanRunPlan } from "./support/runs";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

async function openPicker(page: Page) {
  await page.getByTestId("project-switcher").click();
  const menu = page.getByTestId("project-switcher-menu");
  await expect(menu).toBeVisible();
  return menu;
}

/** The colours a nav item is drawn in, next to the kit's tokens resolved the same way. */
async function itemColours(page: Page, testId: string) {
  return page.getByTestId(testId).evaluate((link) => {
    // One probe per token: Chrome may answer a second read of the same probe from its first style.
    const resolve = (token: string) => {
      const probe = document.createElement("span");
      probe.style.color = `var(${token})`;
      document.body.append(probe);
      const colour = getComputedStyle(probe).color;
      probe.remove();
      return colour;
    };
    const tokens = { brand: resolve("--brand"), selected: resolve("--surface-selected"), muted: resolve("--fg-muted") };
    const icon = link.querySelector("svg");
    return {
      background: getComputedStyle(link).backgroundColor,
      icon: icon ? getComputedStyle(icon).color : "",
      ...tokens,
    };
  });
}

async function widthOf(page: Page, selector: string): Promise<number> {
  return (await page.locator(selector).boundingBox())?.width ?? 0;
}

async function pickerProjects(page: Page): Promise<string[]> {
  const menu = await openPicker(page);
  const names = await menu.locator("[data-project]").evaluateAll((items) =>
    items.map((item) => item.getAttribute("data-project") ?? ""),
  );
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  return names;
}

test.describe("security headers", () => {
  test("/ answers with a nonce CSP and the security headers @deployed", async ({
    page,
    playwright,
    baseURL,
    signInAs,
  }) => {
    // Signed out, / is a redirect to the sign-in page, and that response carries the policy too.
    const anonymous = await playwright.request.newContext({ baseURL, storageState: { cookies: [], origins: [] } });
    const redirect = await anonymous.get("/", { maxRedirects: 0 });
    expect(redirect.status()).toBe(307);
    expect(redirect.headers()["location"]).toMatch(/\/login$/);
    expect(redirect.headers()["content-security-policy"]).toContain("frame-ancestors 'none'");
    await anonymous.dispose();

    if (!isDeployed) await signInAs(newAccount("headers"));
    const blocked: string[] = [];
    page.on("console", (message) => {
      if (/content security policy/i.test(message.text())) blocked.push(message.text());
    });
    const response = await page.goto("/");
    expect(response?.status()).toBe(200);
    const headers = response!.headers();
    const csp = headers["content-security-policy"];
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(cspSources(csp)).not.toContain("'unsafe-eval'"); // 'wasm-unsafe-eval' is a source of its own
    const nonce = /'nonce-([^']+)'/.exec(csp)?.[1];
    expect(nonce, "script-src carries a nonce").toBeTruthy();
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["referrer-policy"]).toBe("same-origin");
    expect(headers["strict-transport-security"]).toContain("max-age=");
    expect(headers["x-frame-options"]).toBe("DENY");
    // Every script in the HTML carries this response's nonce, so the browser runs them; the chunks they load
    // afterwards are trusted through 'strict-dynamic'.
    const html = await response!.text();
    const tags = html.match(/<script\b[^>]*>/g) ?? [];
    expect(tags.length).toBeGreaterThan(0);
    expect(tags.filter((tag) => !tag.includes(`nonce="${nonce}"`))).toEqual([]);
    // And the browser ran them: the shell is interactive.
    await page.getByTestId("user-menu").click();
    await expect(page.getByTestId("user-menu-content")).toBeVisible();
    expect(blocked, "nothing was blocked by the policy").toEqual([]);
    // A second response gets a fresh nonce.
    const again = await page.request.get("/");
    expect(again.headers()["content-security-policy"]).not.toContain(`'nonce-${nonce}'`);
  });
});

test.describe("shell", () => {
  test.skip(isDeployed, "seeds projects and grants through the local stack");

  test("the project picker lists exactly the projects granted to the user", async ({ page, admin, signInAs }) => {
    const account = newAccount("picker");
    const [first, second, other] = [uniqueName("alpha"), uniqueName("beta"), uniqueName("other")];
    for (const name of [first, second, other]) await admin.registerProject(name);
    await admin.grant(first, account.login, "reader", "internal");
    await admin.grant(second, account.login, "writer", "public");

    await signInAs(account);
    expect(await pickerProjects(page)).toEqual([first, second].sort());

    // Home's Projects card lists the same projects, with roles.
    const cards = page.locator("#main").getByTestId("home-projects").getByTestId("home-project");
    await expect(cards).toHaveCount(2);
    expect(await cards.evaluateAll((rows) => rows.map((row) => row.getAttribute("data-project")))).toEqual([first, second].sort());
    await expect(cards.filter({ hasText: first })).toContainText("Đọc, không có plan đang chạy, 1 kho mã");
    await expect(cards.filter({ hasText: second })).toContainText("Ghi");

    // The picker says the same of each project with the Visibility of the grant, and finds one by a part of its name.
    let menu = await openPicker(page);
    await expect(menu.locator(`[data-project="${first}"]`)).toContainText("Đọc, Mức hiển thị: Internal, 1 kho mã, không có plan đang chạy");
    await expect(menu.locator(`[data-project="${second}"]`)).toContainText("Ghi, Mức hiển thị: Public");
    await menu.getByTestId("project-switcher-search").fill(second.split("-").slice(1).join("-"));
    await expect(menu.locator("[data-project]")).toHaveCount(1);
    await expect(menu.locator("[data-project]")).toHaveAttribute("data-project", second);
    await page.keyboard.press("Escape"); // empties the field
    await expect(menu.locator("[data-project]")).toHaveCount(2);
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();

    // Choosing a project opens its overview, and the picker and user menu follow it.
    menu = await openPicker(page);
    await menu.locator(`[data-project="${first}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/p/${first}$`));
    await expect(page.getByRole("heading", { level: 1, name: first })).toBeVisible();
    await expect(page.getByTestId("label-ladder").getByRole("listitem")).toHaveCount(4);
    await expect(page.getByTestId("project-switcher")).toHaveAccessibleName(`Chọn dự án, đang chọn ${first}`);
    await page.getByTestId("user-menu").click();
    await expect(page.getByTestId("user-project-role")).toHaveText(`${first}: Đọc, mức hiển thị Internal`);
  });

  test("a user without grants sees an empty picker and an empty state", async ({ page, signInAs }) => {
    const account = newAccount("nogrants");
    await signInAs(account);
    expect(await pickerProjects(page)).toEqual([]);
    await expect(page.getByTestId("state-empty")).toContainText("Bạn chưa được cấp dự án nào");
    await expect(page.getByTestId("state-empty")).toContainText(account.login);
  });

  test("the user menu shows the login and hub role, and signing out ends the session", async ({ page, member }) => {
    const me = await member();
    await page.getByTestId("user-menu").click();
    await expect(page.getByTestId("user-login")).toHaveText(me.login);
    await expect(page.getByTestId("user-role")).toHaveText("Thành viên");
    await expect(page.getByTestId("nav-admin")).toHaveCount(0);
    await page.getByTestId("logout").click();
    await expect(page).toHaveURL(/\/login$/);
    expect((await page.request.get("/v1/auth/whoami")).status()).toBe(401);
    await page.goto(`/p/${me.projects[0]}`);
    await expect(page).toHaveURL(/\/login$/);
  });

  test("a hub admin sees the admin link and the hub's overview", async ({ page, signInAs }) => {
    await signInAs(ADMIN_ACCOUNT);
    await page.getByTestId("user-menu").click();
    await expect(page.getByTestId("user-role")).toHaveText("Quản trị viên hub");
    await page.keyboard.press("Escape");
    await page.getByTestId("nav-admin").click();
    await expect(page).toHaveURL(/\/admin$/);
    await expect(page.getByRole("heading", { level: 1, name: "Quản trị hub" })).toBeVisible();
    await expect(page.getByTestId("admin-summary")).toContainText("Thành viên");
    await expect(page.getByTestId("admin-attention").getByRole("heading", { level: 2 })).toHaveText("Cần xử lý");
    await expect(page.getByTestId("admin-access").getByRole("heading", { level: 2 })).toHaveText("Quyền theo dự án");
  });

  test("the shell works at 375 px without sideways scrolling", async ({ page, member }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    const me = await member();
    for (const path of ["/", `/p/${me.projects[0]}`, "/admin"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 }).or(page.getByTestId("state-forbidden")).first()).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `${path} scrolls sideways`).toBeLessThanOrEqual(0);
    }
    // The sidebar is a sheet on small screens, opened from the header.
    await page.getByRole("button", { name: "Ẩn hoặc hiện thanh bên" }).click();
    const sheet = page.getByRole("dialog", { name: "Điều hướng chính" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByTestId("fleet-line")).toBeVisible();
    // Touch targets in the sheet are 44 px tall.
    expect((await sheet.getByTestId("nav-inbox").boundingBox())?.height).toBe(44);
    await sheet.getByTestId("nav-home").click();
    await expect(page).toHaveURL(/\/$/);
    await expect(sheet).toBeHidden();
    // The top bar keeps 52 px on a phone, with 44 px controls.
    expect((await page.getByTestId("top-bar").boundingBox())?.height).toBe(52);
    expect((await page.getByTestId("inbox-bell").boundingBox())?.height).toBe(44);
  });

  test("the sidebar is the kit's AppShell: groups, counts in their tones, the fleet line, and a 52 px top bar", async ({
    page,
    member,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedPlanRunPlan(me, project);
    // One worker of the member's holds a plan run that waits for the member's decision.
    await planRunUnderway(me, project, uniqueName("shell"), { waiting: true });

    await open(page, `/p/${project}/runs`);
    const sidebar = page.getByRole("navigation", { name: "Điều hướng chính" });
    await expect(sidebar.getByRole("link")).toHaveCount(15);
    const order = await sidebar.getByRole("link").evaluateAll((links) => links.map((link) => link.getAttribute("data-testid")));
    expect(order).toEqual([
      "nav-home",
      "nav-inbox",
      "nav-monitor",
      "nav-overview",
      "nav-plans",
      "nav-runs",
      "nav-insights",
      "nav-curator",
      "nav-memories",
      "nav-skills",
      "nav-kg",
      "nav-workers",
      "nav-secrets",
      "nav-myMemories",
      "nav-globalSkills",
    ]);
    await expect(page.getByTestId("brand")).toHaveText("evo-agents hub");

    // Inbox counts the open decision (attention), Runs the active run (running); both said in words.
    await expect(page.getByTestId("nav-inbox-count")).toHaveText("1");
    await expect(page.getByTestId("nav-inbox")).toHaveAccessibleName("Inbox, 1 quyết định hoặc đề xuất chờ bạn trả lời");
    await expect(page.getByTestId("nav-runs-count")).toHaveText("1");
    await expect(page.getByTestId("nav-runs")).toHaveAccessibleName("Run, 1 run đang hoạt động");
    // Monitor counts the runs at work: a plan run waiting for a decision is not one.
    await expect(page.getByTestId("nav-monitor")).toHaveAccessibleName("Monitor");
    await expect(page.getByTestId("nav-monitor-count")).toHaveCount(0);

    // The page shown sits on surface-selected with a brand icon; the others stay muted.
    await expect(page.getByTestId("nav-runs")).toHaveAttribute("aria-current", "page");
    const active = await itemColours(page, "nav-runs");
    expect(active.background).toBe(active.selected);
    expect(active.icon).toBe(active.brand);
    const idle = await itemColours(page, "nav-plans");
    expect(idle.icon).toBe(idle.muted);

    // The fleet line reads the member's workers and opens the Workers page.
    const fleet = page.getByTestId("fleet-line");
    await expect(fleet).toHaveText("1 worker online, 1 đang chạy");
    await expect(fleet).toHaveAttribute("data-tone", "success");

    // The top bar is 52 px; the bell's count sits beside the glyph, never over it.
    expect((await page.getByTestId("top-bar").boundingBox())?.height).toBe(52);
    await expect(page.getByTestId("inbox-bell-count")).toHaveText("1");
    const glyph = await page.getByTestId("inbox-bell-glyph").boundingBox();
    const badge = await page.getByTestId("inbox-bell-count").boundingBox();
    expect(glyph && badge).toBeTruthy();
    expect(badge!.x).toBeGreaterThanOrEqual(glyph!.x + glyph!.width);

    // 240 px wide, folding to 56 px of icons from the top bar; the counts become dots, the names tooltips.
    expect(await widthOf(page, "[data-slot=sidebar-container]")).toBe(240);
    const toggle = page.getByTestId("top-bar").getByRole("button", { name: "Ẩn hoặc hiện thanh bên" });
    await toggle.click();
    await expect.poll(() => widthOf(page, "[data-slot=sidebar-container]")).toBe(56);
    await expect(page.getByTestId("nav-inbox-count")).toBeHidden();
    await expect(page.getByTestId("nav-inbox")).toHaveAccessibleName("Inbox, 1 quyết định hoặc đề xuất chờ bạn trả lời");
    await expect(page.getByTestId("brand")).toHaveAccessibleName("evo-agents hub");
    await toggle.click();
    await expect.poll(() => widthOf(page, "[data-slot=sidebar-container]")).toBe(240);

    await fleet.click();
    await expect(page).toHaveURL(/\/workers$/);
    await expect(page.getByTestId("nav-workers")).toHaveAttribute("aria-current", "page");
  });
});
