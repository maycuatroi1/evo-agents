export const LOCALES = ["vi", "en"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "vi";

export function isLocale(value: string | undefined): value is Locale {
  return value !== undefined && (LOCALES as readonly string[]).includes(value);
}

/** The team works from Vietnam; EVO_HUB_WEB_TIME_ZONE overrides it for the dates the server renders. */
export const DEFAULT_TIME_ZONE = "Asia/Ho_Chi_Minh";
