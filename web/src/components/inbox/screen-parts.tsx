"use client";

import type { LucideIcon } from "lucide-react";
import { useFormatter } from "next-intl";
import { type ReactNode } from "react";

import { cn } from "@/lib/utils";

import { type DigestLine } from "./agent-digest";

/** How a line of the agent's digest shows: its icon and tone, who did it, what, and its meta (`useDigestLine`). */
export type DigestView = { icon: LucideIcon; tone: "danger" | "success" | null; who: string; text: ReactNode; meta: string | null };

/** The middle of the screen, the only part that scrolls. */
export function ScreenScroll({ id, children }: { id: number; children: ReactNode }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain" data-testid="decision-panel" data-decision-id={id}>
      <div className="flex flex-col gap-4 px-4 pt-4 pb-6">{children}</div>
    </div>
  );
}

export function DigestItem({ line, view }: { line: DigestLine; view: DigestView }) {
  const format = useFormatter();
  const Icon = view.icon;
  const at = new Date(line.at);
  return (
    <li className="grid grid-cols-[16px_minmax(0,1fr)_auto] items-start gap-x-2.5" data-testid="decision-so-far-item" data-type={line.type}>
      <Icon
        className={cn("mt-0.5 size-4", view.tone === "danger" ? "text-danger" : view.tone === "success" ? "text-success" : "text-fg-subtle")}
        aria-hidden="true"
      />
      <p className="line-clamp-2 min-w-0 text-sm leading-5 text-muted-foreground [overflow-wrap:anywhere]">
        {view.who ? <span className="font-medium text-foreground">{view.who}</span> : null}
        {view.who && view.text ? " " : null}
        {view.text}
        {view.meta ? <span className={cn("text-fg-subtle tabular-nums", view.tone === "danger" && "text-danger")}>{`, ${view.meta}`}</span> : null}
      </p>
      <time dateTime={line.at} title={format.dateTime(at, { dateStyle: "full", timeStyle: "long" })} className="mt-0.5 font-mono text-xs leading-4 text-fg-subtle tabular-nums">
        {format.dateTime(at, { timeStyle: "short" })}
      </time>
    </li>
  );
}
