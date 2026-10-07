import { SESSION_COOKIE } from "./support/auth";
import { WEB_ORIGIN } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount } from "./support/hub";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

const PROTECTED = ["/", "/admin", "/p/some-project", "/p/some-project/plans", "/no/such/page"];

test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("every page sends a visitor without a session to the sign-in page @deployed", async ({ page }) => {
    for (const path of PROTECTED) {
      await page.goto(path);
      await expect(page, `${path} without a session`).toHaveURL(/\/login$/);
      await expect(page.getByRole("heading", { level: 1, name: "Đăng nhập vào evo-agents hub" })).toBeVisible();
    }
    await expect(page.getByTestId("login-github")).toHaveAttribute("href", "/v1/auth/web/login");
  });

  test("a malformed session cookie counts as none", async ({ page, context }) => {
    test.skip(isDeployed, "sets a cookie on the local origin");
    await context.addCookies([{ name: SESSION_COOKIE, value: "not-a-session", url: WEB_ORIGIN }]);
    await page.goto("/admin");
    await expect(page).toHaveURL(/\/login$/);
  });
});

test.describe("signing in", () => {
  test.skip(isDeployed, "needs the fake GitHub of the local stack");

  test("the GitHub web flow ends on the shell with the session cookie set", async ({ page, signInAs, context }) => {
    const account = newAccount("login");
    await signInAs(account);
    await expect(page.getByRole("heading", { level: 1, name: "Trang chủ" })).toBeVisible();
    await expect(page.getByTestId("user-menu")).toContainText(account.login);
    const session = (await context.cookies()).find((cookie) => cookie.name === SESSION_COOKIE);
    expect(session?.httpOnly).toBe(true);
    expect(session?.value).toMatch(/^evs_/);
  });

  test("the sign-in page sends a signed-in visitor home", async ({ page, signInAs }) => {
    await signInAs(newAccount("again"));
    await page.goto("/login");
    await expect(page).toHaveURL(`${WEB_ORIGIN}/`);
  });

  test("a revoked session is refused by the API and the visitor signs in again", async ({ page, signInAs, context }) => {
    await signInAs(newAccount("revoked"));
    const session = (await context.cookies()).find((cookie) => cookie.name === SESSION_COOKIE);
    expect(session).toBeDefined();
    // Sign out through the user menu: the API revokes the session and deletes the cookie.
    await page.getByTestId("user-menu").click();
    await page.getByTestId("logout").click();
    await expect(page).toHaveURL(/\/login$/);
    // Put the revoked value back: the proxy lets it through (it has the right shape), the API says 401.
    await context.addCookies([{ ...session!, expires: -1 }]);
    const whoami = await page.request.get("/v1/auth/whoami");
    expect(whoami.status()).toBe(401);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByTestId("login-github")).toBeVisible();
  });
});
