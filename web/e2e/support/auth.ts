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

/**
 * A page of the app answers its first 401 with window.location.assign("/login") (the query cache, the inbox bell, the
 * fleet line), and that navigation aborts a page.goto("/login") under way with net::ERR_ABORTED. A page that has just
 * loaded can still be hydrating, its first requests not sent yet, so a spec that only waited for server-rendered
 * content must not clear the cookies under it. Both helpers below leave the app for about:blank first.
 */
async function leaveTheApp(page: Page): Promise<void> {
  if (page.url() !== "about:blank") await page.goto("about:blank");
}

/** Sign the browser out of the hub and the fake GitHub, from a blank page, so no page of the app sees the 401. */
export async function signOut(page: Page): Promise<void> {
  await leaveTheApp(page);
  await page.context().clearCookies();
}

/** The whole web flow: sign-in page, GitHub button, fake github.com, callback, back on the shell at /. */
export async function signIn(page: Page, account: Account): Promise<void> {
  await leaveTheApp(page);
  await signInToFakeGitHub(page.context(), account);
  await page.goto("/login");
  await page.getByTestId("login-github").click();
  await page.waitForURL((url) => url.pathname === "/");
  await expect(page.locator("#main")).toBeVisible(); // the sidebar, and the user menu in it, is a sheet on phones
}
