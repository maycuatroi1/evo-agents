import { cookies } from "next/headers";
import { getRequestConfig } from "next-intl/server";

import { LOCALE_COOKIE } from "@/lib/config";

import { DEFAULT_TIME_ZONE, requestLocale } from "./locales";

/**
 * next-intl without locale routing: the language comes from the cookie the user menu sets, else from
 * EVO_HUB_WEB_DEFAULT_LOCALE (read at runtime, `en` or `vi`), else English. Accept-Language is not read, so a page
 * renders the same for every browser.
 */
export default getRequestConfig(async () => {
  const saved = (await cookies()).get(LOCALE_COOKIE)?.value;
  const locale = requestLocale(saved, process.env.EVO_HUB_WEB_DEFAULT_LOCALE);
  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
    timeZone: process.env.EVO_HUB_WEB_TIME_ZONE?.trim() || DEFAULT_TIME_ZONE,
  };
});
