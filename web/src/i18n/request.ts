import { cookies } from "next/headers";
import { getRequestConfig } from "next-intl/server";

import { LOCALE_COOKIE } from "@/lib/config";

import { DEFAULT_LOCALE, DEFAULT_TIME_ZONE, isLocale } from "./locales";

/** next-intl without locale routing: the language comes from a cookie, Vietnamese by default. */
export default getRequestConfig(async () => {
  const saved = (await cookies()).get(LOCALE_COOKIE)?.value;
  const locale = isLocale(saved) ? saved : DEFAULT_LOCALE;
  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
    timeZone: process.env.EVO_HUB_WEB_TIME_ZONE?.trim() || DEFAULT_TIME_ZONE,
  };
});
