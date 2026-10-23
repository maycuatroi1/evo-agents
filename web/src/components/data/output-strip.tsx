"use client";

import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useRef, useState } from "react";

import type { ViewedFile } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import { OutputChip } from "./output-tile";
import type { OutputViewerProps } from "./output-viewer";

/** The viewer, loaded the first time a file is opened: a page that only lists files carries none of it. */
const LazyViewer = dynamic(() => import("./output-viewer").then((module) => module.OutputViewer), { ssr: false }) as <F extends ViewedFile>(
  props: OutputViewerProps<F>,
) => ReactNode;

/**
 * Which output the viewer shows (null: closed), and the viewer itself once one was opened, kept mounted afterwards so
 * it closes as a dialog does and gives focus back to the button that opened it.
 */
export function useOutputViewer<F extends ViewedFile>(outputs: readonly F[], actions?: OutputViewerProps<F>["actions"]) {
  const [openId, setOpenId] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  if (openId !== null && !loaded) setLoaded(true);
  // The button that opened the viewer, which takes focus back when it closes (the dialog has no trigger of its own).
  const opener = useRef<HTMLElement | null>(null);
  const open = useCallback((id: number | null) => {
    const active = document.activeElement;
    if (id !== null && active instanceof HTMLElement && !active.closest('[data-testid="output-viewer"]')) opener.current = active;
    setOpenId(id);
  }, []);
  const returnFocus = useCallback(() => opener.current, []);
  const viewer = loaded ? <LazyViewer outputs={outputs} openId={openId} onOpenChange={open} actions={actions} returnFocus={returnFocus} /> : null;
  return { openId, open, viewer };
}

/** At most this many chips show; the rest are one button that opens the viewer on the first of them. */
const SHOWN = 8;

/**
 * The files a trace row, a question, a chat message or a notice carries, as a list of chips (an image as its
 * thumbnail, any other file as its icon, name and size) that open the viewer over all of them. `onOpen` lets a page
 * hold one viewer for its strips (the run page); without it the strip holds its own.
 */
export function OutputStrip<F extends ViewedFile>({
  outputs,
  label,
  onOpen,
  className,
  testId = "output-strip",
}: {
  outputs: readonly F[];
  /** The list's accessible name: "Files of this question". */
  label: string;
  onOpen?: (output: F, list: readonly F[]) => void;
  className?: string;
  testId?: string;
}): ReactNode {
  const t = useTranslations("outputs");
  const own = useOutputViewer(outputs);
  if (outputs.length === 0) return null;
  const open = (output: F) => (onOpen ? onOpen(output, outputs) : own.open(output.id));
  const shown = outputs.slice(0, SHOWN);
  const rest = outputs.length - shown.length;
  return (
    <>
      <ul aria-label={label} className={cn("flex min-w-0 flex-wrap items-center gap-1.5", className)} data-testid={testId}>
        {shown.map((output) => (
          <li key={output.id} className="min-w-0">
            <OutputChip output={output} onOpen={() => open(output)} />
          </li>
        ))}
        {rest > 0 ? (
          <li>
            <button
              type="button"
              onClick={() => open(outputs[SHOWN])}
              className="h-9 cursor-pointer rounded-sm border bg-card px-2.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring max-md:min-h-11"
              data-testid="output-strip-more"
            >
              {t("more", { count: rest })}
            </button>
          </li>
        ) : null}
      </ul>
      {onOpen ? null : own.viewer}
    </>
  );
}
