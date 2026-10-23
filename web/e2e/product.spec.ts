import { readFileSync } from "node:fs";

import type { Page } from "@playwright/test";

import type enMessages from "../messages/en.json";
import { expect, test } from "./support/fixtures";
import { newAccount } from "./support/hub";

/**
 * The public product page: what a visitor without a session gets at `/` (and at /about, its address to share), what
 * crawlers may read, and that every other page still sends that visitor to the sign-in page. A member gets Home at `/`.
 * Local stack only: a deployed hub has the page once a release with it is deployed there.
 */
/** The page's copy in a locale, read as a file: Playwright loads the specs as ES modules, where JSON needs an attribute. */
function productCopy(locale: "en" | "vi"): (typeof enMessages)["product"] {
  const file = new URL(`../messages/${locale}.json`, import.meta.url);
  return (JSON.parse(readFileSync(file, "utf8")) as typeof enMessages).product;
}

const PRODUCT = productCopy("en");
const PRODUCT_VI = productCopy("vi");
const OTHER_PAGES = ["/admin", "/inbox", "/p/some-project", "/p/some-project/plans", "/about/more", "/no/such/page"];

/** The paths of /v1 requests the browser makes while the page loads and settles. */
async function apiCalls(page: Page, path: string): Promise<string[]> {
  const calls: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/v1/")) calls.push(url.pathname);
  });
  await page.goto(path);
  await page.waitForLoadState("networkidle");
  return calls;
}

const meta = (page: Page, selector: string) => page.locator(`head ${selector}`).getAttribute("content");

test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a visitor at / gets the product page under the same address, calling no API", async ({ page }) => {
    const response = await page.request.get("/", { maxRedirects: 0 });
    expect(response.status()).toBe(200);
    expect(response.headers()["content-security-policy"]).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
    expect(await apiCalls(page, "/")).toEqual([]);
    expect(new URL(page.url()).pathname).toBe("/");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(PRODUCT.hero.title);
    await expect(page.getByTestId("product-sign-in")).toHaveAttribute("href", "/v1/auth/web/login");
    await expect(page.getByTestId("product-sign-in")).toHaveText(PRODUCT.hero.signIn);
    await expect(page.getByTestId("product-invite")).toHaveAttribute(
      "href",
      "https://github.com/maycuatroi1/evo-agents/issues/new?template=invite.yml",
    );
    await expect(page.getByTestId("product-source")).toHaveAttribute("href", "https://github.com/maycuatroi1/evo-agents");
    await expect(page.getByTestId("product-login-command")).toContainText(`evo-agents hub login --url ${new URL(page.url()).origin}`);
  });

  test("the page has an address of its own that anyone can open", async ({ page }) => {
    const response = await page.goto("/about");
    expect(response?.status()).toBe(200);
    await expect(page).toHaveURL(/\/about$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(PRODUCT.hero.title);
  });

  test("every other page still sends the visitor to the sign-in page", async ({ page }) => {
    test.setTimeout(60_000);
    for (const path of OTHER_PAGES) {
      await page.goto(path);
      await expect(page, `${path} without a session`).toHaveURL(/\/login$/);
    }
  });

  test("only the product page asks to be indexed, with a description and its OpenGraph image", async ({ page }) => {
    await page.goto("/");
    const origin = new URL(page.url()).origin;
    await expect(page).toHaveTitle(PRODUCT.meta.title);
    expect(await meta(page, 'meta[name="robots"]')).toBe("index, follow");
    expect(await meta(page, 'meta[name="description"]')).toBe(PRODUCT.meta.description);
    // Next.js writes the root URL without its trailing slash; parsed, it is the same URL.
    const canonical = await page.locator('head link[rel="canonical"]').getAttribute("href");
    expect(canonical).not.toBeNull();
    expect(new URL(canonical!).href).toBe(`${origin}/`);
    expect(await meta(page, 'meta[property="og:image"]')).toBe(`${origin}/product/og.png`);
    expect(await meta(page, 'meta[property="og:image:width"]')).toBe("1200");
    expect(await meta(page, 'meta[property="og:image:height"]')).toBe("630");
    expect(await meta(page, 'meta[property="og:image:alt"]')).toBe(PRODUCT.meta.imageAlt);
    expect(await meta(page, 'meta[name="twitter:card"]')).toBe("summary_large_image");
    const image = await page.request.get("/product/og.png");
    expect(image.status()).toBe(200);
    expect(image.headers()["content-type"]).toBe("image/png");

    await page.goto("/login");
    expect(await meta(page, 'meta[name="robots"]')).toBe("noindex, nofollow");
    expect(await page.locator('head meta[property="og:image"]').count()).toBe(0);
  });

  test("robots.txt lets crawlers read the product page and nothing else", async ({ request }) => {
    const response = await request.get("/robots.txt", { maxRedirects: 0 });
    expect(response.status()).toBe(200);
    const lines = (await response.text()).split("\n").map((line) => line.trim());
    expect(lines).toContain("User-Agent: *");
    for (const allowed of ["/$", "/about$", "/product/", "/_next/static/"]) expect(lines).toContain(`Allow: ${allowed}`);
    expect(lines).toContain("Disallow: /");
  });

  test.describe("in Vietnamese", () => {
    test.use({ uiLocale: "vi" });

    test("the page reads in the visitor's language", async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(PRODUCT_VI.hero.title);
      await expect(page.getByTestId("product-sign-in")).toHaveText(PRODUCT_VI.hero.signIn);
    });
  });
});

test("a member gets Home at / and can still open the product page", async ({ page, signInAs }) => {
  test.setTimeout(60_000);
  await signInAs(newAccount("product"));
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Home", exact: true })).toBeVisible();
  await page.goto("/about");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(PRODUCT.hero.title);
  await expect(page.getByTestId("product-sign-in")).toHaveCount(0);
  await page.getByTestId("product-open-hub").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { level: 1, name: "Home", exact: true })).toBeVisible();
});
