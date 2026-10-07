import { useSyncExternalStore } from "react";

/**
 * The key that goes with Enter to send a form, as the visitor's keyboard labels it: Command on Apple devices, Ctrl
 * elsewhere. Null on the server and during hydration, so the two renders agree and the hint appears once the browser
 * knows; the shortcut itself works either way, as a form takes Meta or Ctrl with Enter.
 */
export type ModifierKey = "meta" | "ctrl";

const APPLE = /Mac|iPhone|iPad|iPod/i;

function platform(): string {
  const data = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  return data?.platform || navigator.platform || navigator.userAgent;
}

const subscribe = () => () => {};
const client = (): ModifierKey => (APPLE.test(platform()) ? "meta" : "ctrl");
const server = (): ModifierKey | null => null;

export function useModifierKey(): ModifierKey | null {
  return useSyncExternalStore(subscribe, client, server);
}

/** Whether a key press is the send shortcut: Enter with Command or Ctrl, nothing else held and not mid-composition. */
export function isSendShortcut(event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "isComposing">): boolean {
  return event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && !event.isComposing;
}
