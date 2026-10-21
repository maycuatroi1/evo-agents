"use client";

import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

import type { LogStatus } from "./use-run-log";

/**
 * The stream's state as the kit's live state beside the tabs, shown only when the stream dropped (reconnecting, read by
 * polling, failed): a dot in its tone and a word.
 */
const STATUS_DOT: Record<LogStatus, string> = {
  connecting: "bg-neutral-solid",
  live: "bg-success-solid",
  reconnecting: "bg-attention-solid",
  polling: "bg-attention-solid",
  ended: "bg-neutral-solid",
  failed: "bg-danger-solid",
};

export function LogStreamState({ status }: { status: LogStatus }) {
  const t = useTranslations("runs.detail.log.status");
  return (
    <span
      // The usual states (connecting, live, ended) are said to screen readers only; a dropped stream shows.
      className={cn("inline-flex items-center gap-2 text-xs whitespace-nowrap text-fg-subtle", (status === "live" || status === "connecting" || status === "ended") && "sr-only")}
      data-testid="log-status"
      data-status={status}
    >
      <span className="relative inline-flex size-2 shrink-0" aria-hidden="true">
        <span className={cn("size-2 rounded-full", STATUS_DOT[status])} />
        {status === "live" ? <span className={cn("absolute inset-0 animate-live-ping rounded-full", STATUS_DOT.live)} /> : null}
      </span>
      {t(status)}
    </span>
  );
}
