import { type BrowserContext, expect, type Page } from "@playwright/test";

import type { Account } from "./hub";

export const FAKE_GITHUB_COOKIE = "fake_github_login";
export const SESSION_COOKIE = "evo_hub_session";

/** Sign this browser in to the fake github.com, as if the person had a GitHub session already. */
export async function signInToFakeGitHub(context: BrowserContext, account: Account): Promise<void> {
  await context.addCookies([
    { name: FAKE_GITHUB_COOKIE, value: `${account.login}:${account.id}`, domain: "127.0.0.1", path: "/" },
  ]);
}

/** The whole web flow: sign-in page, GitHub button, fake github.com, callback, back on the shell at /. */
export async function signIn(page: Page, account: Account): Promise<void> {
  await signInToFakeGitHub(page.context(), account);
  await page.goto("/login");
  await page.getByTestId("login-github").click();
  await page.waitForURL((url) => url.pathname === "/");
  await expect(page.locator("#main")).toBeVisible(); // the sidebar, and the user menu in it, is a sheet on phones
}
