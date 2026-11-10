"use client";

import { createContext, useContext } from "react";

/**
 * Opens a thread of the chat shown, beside it or in its sheet, from what the page puts in the chat (the work card's
 * sub-tasks and its ask cards): null outside a chat's page.
 */
export const OpenThreadContext = createContext<((root: number) => void) | null>(null);

export function useOpenThread(): ((root: number) => void) | null {
  return useContext(OpenThreadContext);
}
