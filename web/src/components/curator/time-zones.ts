/** The time zones of the charter form, from the browser. */

/** The time zones the browser knows, for the field's suggestions; the hub checks the one written against Postgres. */
export function timeZones(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

/** The visitor's own time zone, for a first charter. */
export function ownTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}
