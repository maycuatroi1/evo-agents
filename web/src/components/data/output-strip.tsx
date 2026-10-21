"use client";

import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import type { RunOutput } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import { OutputChip } from "./output-tile";
import type { OutputViewerProps } from "./output-viewer";

/** The viewer, loaded the first time a file is opened: a page that only lists files carries none of it. */
const LazyViewer = dynamic(() => import("./output-viewer").then((module) => module.OutputViewer), { ssr: false });

/**
 * Which output the viewer shows (null: closed), and the viewer itself once one was opened, kept mounted afterwards so
 * it closes as a dialog does and gives focus back to the button that opened it.
 */
export function useOutputViewer(outputs: readonly RunOutput[], actions?: OutputViewerProps["actions"]) {
  const [openId, setOpenId] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  if (openId !== null && !loaded) setLoaded(true);
  const viewer = loaded ? <LazyViewer outputs={outputs} openId={openId} onOpenChange={setOpenId} actions={actions} /> : null;
  return { openId, open: setOpenId, viewer };
}

/** At most this many chips show; the rest are one button that opens the viewer on the first of them. */
const SHOWN = 8;

/**
 * The files a trace row, a question, a chat message or a notice carries, as a list of chips (an image as its
 * thumbnail, any other file as its icon, name and size) that open the viewer over all of them. `onOpen` lets a page
 * hold one viewer for its strips (the run page); without it the strip holds its own.
 */
export function OutputStrip({
  outputs,
  label,
  onOpen,
  className,
  testId = "output-strip",
}: {
  outputs: readonly RunOutput[];
  /** The list's accessible name: "Files of this question". */
  label: string;
  onOpen?: (output: RunOutput, list: readonly RunOutput[]) => void;
  className?: string;
  testId?: string;
}): ReactNode {
  const t = useTranslations("outputs");
  const own = useOutputViewer(outputs);
  if (outputs.length === 0) return null;
  const open = (output: RunOutput) => (onOpen ? onOpen(output, outputs) : own.open(output.id));
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
