"use client";

import { useTranslations } from "next-intl";
import { type KeyboardEvent, type PointerEvent, type RefObject, useCallback, useState, useSyncExternalStore } from "react";

/** Where the browser keeps the share of the chat's width the visitor gave the thread column. */
export const THREAD_WIDTH_KEY = "evo.chats.threadWidth";
/** The thread column's share of the chat's width: wide enough to read and answer at once by default. */
export const THREAD_WIDTH = { min: 0.3, max: 0.7, initial: 0.45, step: 0.05 } as const;

export function clampWidth(share: number): number {
  return Math.min(THREAD_WIDTH.max, Math.max(THREAD_WIDTH.min, share));
}

function storedWidth(): number {
  try {
    const held = Number(window.localStorage.getItem(THREAD_WIDTH_KEY));
    return held > 0 ? clampWidth(held) : THREAD_WIDTH.initial;
  } catch {
    return THREAD_WIDTH.initial;
  }
}

const listeners = new Set<() => void>();

function storeWidth(share: number) {
  try {
    window.localStorage.setItem(THREAD_WIDTH_KEY, share.toFixed(3));
  } catch {
    // A browser that keeps nothing keeps the default.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(onChange: () => void) {
  listeners.add(onChange);
  window.addEventListener("storage", onChange);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

/**
 * The thread column's width as a share of the chat view, kept in the browser so the next thread opens as wide (and a
 * second tab follows). The server renders the default share; the visitor's own takes over right after hydration.
 */
export function useThreadWidth() {
  const share = useSyncExternalStore(subscribe, storedWidth, () => THREAD_WIDTH.initial);
  const resize = useCallback((next: number) => storeWidth(clampWidth(next)), []);
  return { share, resize };
}

/**
 * The bar between the chat and the thread column: dragged with a pointer, or focused and moved with the arrows (Home
 * and End for the narrowest and widest). A window splitter in the ARIA sense, its value the thread's share in percent.
 */
export function ThreadResizeHandle({
  frame,
  share,
  onResize,
}: {
  frame: RefObject<HTMLElement | null>;
  share: number;
  onResize: (share: number) => void;
}) {
  const t = useTranslations("chats.thread");
  const [dragging, setDragging] = useState(false);

  const shareAt = (clientX: number) => {
    const box = frame.current?.getBoundingClientRect();
    return box && box.width > 0 ? (box.right - clientX) / box.width : share;
  };
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (dragging) onResize(shareAt(event.clientX));
  };
  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setDragging(false);
  };
  // The bar sits left of the thread: Left widens it, Right narrows it.
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const moves: Record<string, number> = {
      ArrowLeft: share + THREAD_WIDTH.step,
      ArrowRight: share - THREAD_WIDTH.step,
      Home: THREAD_WIDTH.min,
      End: THREAD_WIDTH.max,
    };
    const next = moves[event.key];
    if (next === undefined) return;
    event.preventDefault();
    onResize(next);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t("resize")}
      aria-valuemin={Math.round(THREAD_WIDTH.min * 100)}
      aria-valuemax={Math.round(THREAD_WIDTH.max * 100)}
      aria-valuenow={Math.round(share * 100)}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown}
      onDoubleClick={() => onResize(THREAD_WIDTH.initial)}
      className="group relative -mx-4 flex w-4 shrink-0 cursor-col-resize touch-none justify-center outline-none select-none"
      data-testid="chat-thread-resize"
      data-dragging={dragging}
    >
      <span
        aria-hidden="true"
        className="my-auto h-12 w-1 rounded-full bg-border transition-colors group-hover:bg-brand group-focus-visible:bg-brand group-data-[dragging=true]:bg-brand"
      />
    </div>
  );
}
