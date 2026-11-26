"use client";

import { createContext, useContext } from "react";

/**
 * Opens a task (its work chat, by id) in Home's main pane, from the direct chat Home shows (web/DESIGN.md, Chat home):
 * there a message that opened work shows one task card, which opens the task beside the rail. Null outside Home, where
 * the direct chat shows every message of its threads.
 */
export const HomeTaskContext = createContext<((chatId: number) => void) | null>(null);

export function useHomeTask(): ((chatId: number) => void) | null {
  return useContext(HomeTaskContext);
}
