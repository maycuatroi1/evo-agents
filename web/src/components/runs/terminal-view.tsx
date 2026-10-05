"use client";

import "@wterm/dom/css";

import { GhosttyCore } from "@wterm/ghostty";
import { Terminal, type TerminalHandle, type WTerm } from "@wterm/react";
import { type Ref, useEffect, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * The terminal itself: wterm's DOM renderer over libghostty's VT core (WebAssembly, fetched from the web's own
 * origin), sized to its box. Loaded only when a person connects (`next/dynamic` in run-terminal.tsx), so the run page
 * does not carry it otherwise. The colours are the log's terminal surface (`hub-terminal` in globals.css).
 *
 * The worker replays up to 256 KiB of what the terminal printed when a browser connects, so each session gets a
 * fresh view; the core goes with it.
 */

/** Bytes of history libghostty keeps; tmux keeps the agent's own. */
const SCROLLBACK_BYTES = 1024 * 1024;

export type TerminalViewProps = {
  handle: Ref<TerminalHandle>;
  label: string;
  describedBy?: string;
  /** Painting stops while the tab is hidden; output is still parsed. */
  paused: boolean;
  onReady: (cols: number, rows: number) => void;
  onData: (data: string) => void;
  onBinary: (data: Uint8Array) => void;
  onResize: (cols: number, rows: number) => void;
  onError: (error: unknown) => void;
  className?: string;
};

export default function TerminalView({
  handle,
  label,
  describedBy,
  paused,
  onReady,
  onData,
  onBinary,
  onResize,
  onError,
  className,
}: TerminalViewProps) {
  const [core, setCore] = useState<GhosttyCore | null>(null);

  useEffect(() => {
    let current: GhosttyCore | null = null;
    let alive = true;
    GhosttyCore.load({ scrollbackLimit: SCROLLBACK_BYTES }).then(
      (loaded) => {
        if (!alive) {
          loaded.dispose();
          return;
        }
        current = loaded;
        setCore(loaded);
      },
      (error: unknown) => {
        if (alive) onError(error);
      },
    );
    return () => {
      alive = false;
      current?.dispose();
    };
    // One core per mounted view; a new session mounts a new view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!core) return <div className={cn("hub-terminal", className)} aria-busy="true" data-testid="terminal-loading" />;
  return (
    <Terminal
      ref={handle}
      core={core}
      autoResize
      renderingPaused={paused}
      className={cn("hub-terminal", className)}
      aria-label={label}
      aria-describedby={describedBy}
      onReady={(term: WTerm) => onReady(term.cols, term.rows)}
      onData={onData}
      onBinary={onBinary}
      onResize={onResize}
      onError={onError}
      data-testid="terminal-screen"
    />
  );
}
