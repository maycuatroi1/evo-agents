import "server-only";

import { headers } from "next/headers";

/**
 * The origin the visitor asked for, from the reverse proxy's forwarded headers when it sets them: the hub URL of the
 * CLI's commands until the browser says its own, and the base of a public page's absolute URLs (its OpenGraph image).
 */
export async function requestOrigin(): Promise<string> {
  const list = await headers();
  const first = (name: string) => list.get(name)?.split(",")[0]?.trim() || null;
  const host = first("x-forwarded-host") ?? first("host") ?? "localhost";
  return `${first("x-forwarded-proto") ?? "http"}://${host}`;
}
