import { LOCALE_COOKIE } from "../src/lib/config";

import { BASE_URL } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount } from "./support/hub";

/**
 * The UI language: English unless the visitor picked another one, which the user menu keeps in the locale cookie.
 * The browser of these specs asks for Vietnamese (Accept-Language vi-VN, from playwright.config.ts) on purpose:
 * the web does not read that header, so only the cookie turns the page Vietnamese.
 */
test.use({ locale: "vi-VN" });

test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("without a locale cookie the sign-in page is in English @deployed", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page).toHaveTitle("Sign in | evo-agents hub");
    await expect(page.getByRole("heading", { level: 1, name: "Sign in to evo-agents hub" })).toBeVisible();
    await expect(page.getByTestId("login-github")).toHaveText("Sign in with GitHub");
  });

  test.describe("with the locale cookie set to vi", () => {
    test.use({ uiLocale: "vi" });

    test("the sign-in page is in Vietnamese @deployed", async ({ page }) => {
      await page.goto("/login");
      await expect(page.locator("html")).toHaveAttribute("lang", "vi");
      await expect(page).toHaveTitle("Đăng nhập | evo-agents hub");
      await expect(page.getByRole("heading", { level: 1, name: "Đăng nhập vào evo-agents hub" })).toBeVisible();
      await expect(page.getByTestId("login-github")).toHaveText("Đăng nhập bằng GitHub");
    });
  });

  test("a locale cookie that names no supported language counts as none", async ({ page, context }) => {
    await context.addCookies([{ name: LOCALE_COOKIE, value: "fr", url: BASE_URL, sameSite: "Lax" }]);
    await page.goto("/login");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page.getByRole("heading", { level: 1, name: "Sign in to evo-agents hub" })).toBeVisible();
  });
});

test.describe("choosing the language", () => {
  test.skip(isDeployed, "needs the fake GitHub of the local stack");

  test("the user menu switches to Vietnamese and back, and the choice outlives a reload", async ({
    page,
    context,
    signInAs,
  }) => {
    await signInAs(newAccount("locale"));
    await expect(page.getByRole("heading", { level: 1, name: "My projects" })).toBeVisible();

    await page.getByTestId("user-menu").click();
    await page.getByRole("menuitem", { name: "Language" }).click();
    const english = page.getByRole("menuitemradio", { name: "English" });
    await expect(english).toHaveAttribute("aria-checked", "true");
    await expect(english).toHaveAttribute("lang", "en");
    await page.getByRole("menuitemradio", { name: "Tiếng Việt" }).click();

    await expect(page.getByRole("heading", { level: 1, name: "Dự án của tôi" })).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "vi");
    const cookie = (await context.cookies()).find((saved) => saved.name === LOCALE_COOKIE);
    expect(cookie?.value).toBe("vi");
    expect(cookie?.sameSite).toBe("Lax");

    await page.reload();
    await expect(page.getByRole("heading", { level: 1, name: "Dự án của tôi" })).toBeVisible();

    await page.getByTestId("user-menu").click();
    await page.getByRole("menuitem", { name: "Ngôn ngữ" }).click();
    await page.getByRole("menuitemradio", { name: "English" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "My projects" })).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
  });
});
