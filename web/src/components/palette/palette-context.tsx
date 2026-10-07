"use client";

import dynamic from "next/dynamic";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { takesText, useModifierKey } from "@/lib/keyboard";

/**
 * The command palette's state, in the shell: whether it is open, what held focus when it opened, and Cmd K or Ctrl K
 * anywhere. The palette itself (cmdk, its queries and the dialogs it opens) is its own chunk, fetched when the pointer
 * or focus reaches the top bar's field or at the first shortcut, so no page carries it in its first load.
 */

const loadPalette = () => import("./command-palette");
const LazyCommandPalette = dynamic(() => loadPalette().then((module) => module.CommandPalette), { ssr: false });

/** Fetches the palette's code before it is first opened. */
export const preloadPalette = () => void loadPalette();

/** `from` is what focus goes back to when the palette closes; by default what holds focus as it opens. */
type PaletteState = { open: boolean; openPalette: (from?: HTMLElement | null) => void };

const PaletteContext = createContext<PaletteState | null>(null);

/** A dialog, sheet or popover open while the palette is closed: the shortcut leaves it alone. */
const OPEN_OVERLAY = '[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]';

/**
 * Cmd K or Ctrl K with nothing else held. On an Apple keyboard Ctrl K in a text field stays the field's own (it cuts to
 * the end of the line there), and in the web terminal the key is always the terminal's.
 */
export function isPaletteShortcut(
  event: Pick<KeyboardEvent, "key" | "code" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "isComposing" | "target">,
  apple: boolean,
): boolean {
  if (event.altKey || event.shiftKey || event.isComposing) return false;
  if ((event.key ?? "").toLowerCase() !== "k" && event.code !== "KeyK") return false;
  if (event.metaKey === event.ctrlKey) return false;
  if (event.target instanceof Element && event.target.closest(".hub-terminal")) return false;
  return !(apple && event.ctrlKey && takesText(event.target));
}

export function CommandPaletteProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const isOpen = useRef(false);
  const apple = useModifierKey() === "meta";

  const openPalette = useCallback((from?: HTMLElement | null) => {
    const active = document.activeElement;
    opener.current = from ?? (active instanceof HTMLElement && active !== document.body ? active : null);
    isOpen.current = true;
    setMounted(true);
    setOpen(true);
  }, []);

  const changeOpen = useCallback((next: boolean) => {
    isOpen.current = next;
    setOpen(next);
  }, []);

  /** Focus goes back to what held it when the palette opened, if it is still on the page. */
  const restoreFocus = useCallback(() => {
    const target = opener.current;
    opener.current = null;
    if (target?.isConnected) target.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || !isPaletteShortcut(event, apple)) return;
      if (isOpen.current) {
        event.preventDefault();
        changeOpen(false);
        return;
      }
      if (document.querySelector(OPEN_OVERLAY)) return;
      event.preventDefault(); // the browser's own Ctrl K (its search bar) does not open as well
      openPalette();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [apple, changeOpen, openPalette]);

  const value = useMemo(() => ({ open, openPalette }), [open, openPalette]);
  return (
    <PaletteContext.Provider value={value}>
      {children}
      {mounted ? <LazyCommandPalette open={open} onOpenChange={changeOpen} restoreFocus={restoreFocus} /> : null}
    </PaletteContext.Provider>
  );
}

export function usePalette(): PaletteState {
  const state = useContext(PaletteContext);
  if (!state) throw new Error("usePalette needs CommandPaletteProvider");
  return state;
}
