import * as React from "react";

const MOBILE_BREAKPOINT = 768;
const QUERY = `(max-width: ${MOBILE_BREAKPOINT - 1}px)`;

/** Whether the browser can answer media queries (jsdom in unit tests cannot). */
function canMatch(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function";
}

function subscribe(onChange: () => void): () => void {
  if (!canMatch()) return () => {};
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

function snapshot(): boolean {
  return canMatch() && window.matchMedia(QUERY).matches;
}

/**
 * The server's guess that the visitor is on a phone, from the request's user agent (`mobileHint` in
 * `app/layout.tsx`). The server cannot measure the viewport, so the page it renders, and the first render in the
 * browser that hydrates it, follow this guess; right after hydration the browser's own answer takes over. A good guess
 * means a phone gets the phone layout (a list instead of a table, filters in a sheet) in the HTML itself, with no swap
 * once the scripts run.
 */
const MobileHint = React.createContext(false);

export function MobileHintProvider({ mobile, children }: { mobile: boolean; children: React.ReactNode }) {
  return React.createElement(MobileHint.Provider, { value: mobile }, children);
}

/**
 * Below the md breakpoint (768 px), where the sidebar becomes a sheet, tables become lists and filters move into a
 * sheet. While rendering on the server and hydrating, the server's guess (`MobileHintProvider`).
 */
export function useIsMobile(): boolean {
  const hint = React.useContext(MobileHint);
  return React.useSyncExternalStore(subscribe, snapshot, () => hint);
}
