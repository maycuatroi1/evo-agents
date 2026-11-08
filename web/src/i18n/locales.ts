/** English first; Vietnamese stays one pick away in the user menu. The order is the menu's order. */
export const LOCALES = ["en", "vi"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";

export function isLocale(value: string | undefined): value is Locale {
  return value !== undefined && (LOCALES as readonly string[]).includes(value);
}

/**
 * The language of a request: the one the user menu saved in the cookie, else the hub's default
 * (EVO_HUB_WEB_DEFAULT_LOCALE, `en` or `vi`), else English. A value that is not a locale counts as none.
 */
export function requestLocale(saved: string | undefined, hubDefault: string | undefined): Locale {
  if (isLocale(saved)) return saved;
  const fallback = hubDefault?.trim().toLowerCase();
  return isLocale(fallback) ? fallback : DEFAULT_LOCALE;
}

/** The team works from Vietnam; EVO_HUB_WEB_TIME_ZONE overrides it for the dates the server renders. */
export const DEFAULT_TIME_ZONE = "Asia/Ho_Chi_Minh";
