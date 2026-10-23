/**
 * The path of the page of the invite link `token`, which INVITE_PAGE in config.ts lets through without a session.
 * Its own module, so the pages that link to it leave the config every page loads as it was.
 */
export function invitePath(token: string): string {
  return `/invite/${encodeURIComponent(token)}`;
}
