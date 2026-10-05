import type { BrowserContext } from "@playwright/test";

import type { Locale } from "../../src/i18n/locales";
import { LOCALE_COOKIE } from "../../src/lib/config";

import { BASE_URL } from "./env";

/**
 * Pick the UI language of every page in `context` the way the user menu does: the locale cookie on the web's
 * origin. Without it the web renders English (`DEFAULT_LOCALE`), whatever language the browser asks for.
 */
export async function setUiLocale(context: BrowserContext, locale: Locale): Promise<void> {
  await context.addCookies([{ name: LOCALE_COOKIE, value: locale, url: BASE_URL, sameSite: "Lax" }]);
}
