import { userAgentFromString } from "next/server";

/**
 * Whether a request most likely comes from a phone: Chromium's `Sec-CH-UA-Mobile: ?1`, or a user agent that names a
 * phone (not a tablet, which is at least 768 px wide). The server renders the phone layout on this guess (`useIsMobile`
 * in `hooks/use-mobile.ts`); the browser corrects a wrong one right after hydration.
 */
export function mobileHint(headers: Pick<Headers, "get">): boolean {
  if (headers.get("sec-ch-ua-mobile") === "?1") return true;
  return userAgentFromString(headers.get("user-agent") ?? undefined).device.type === "mobile";
}
