"use client";

import { Ban, CircleCheck, Loader2, type LucideIcon, Pause, WifiOff } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

import type { WorkerView } from "./model";

const VIEWS: Record<WorkerView, { icon: LucideIcon; variant: "success" | "info" | "warning" | "outline" | "secondary" }> = {
  idle: { icon: CircleCheck, variant: "success" },
  busy: { icon: Loader2, variant: "info" },
  draining: { icon: Pause, variant: "warning" },
  offline: { icon: WifiOff, variant: "secondary" },
  revoked: { icon: Ban, variant: "outline" },
};

export const VIEW_ICONS: Record<WorkerView, LucideIcon> = {
  idle: VIEWS.idle.icon,
  busy: VIEWS.busy.icon,
  draining: VIEWS.draining.icon,
  offline: VIEWS.offline.icon,
  revoked: VIEWS.revoked.icon,
};

/** A worker's status as an icon and a word, so colour is never the only signal. */
export function WorkerStatusBadge({ view, className }: { view: WorkerView; className?: string }) {
  const t = useTranslations("workers.status");
  const { icon: Icon, variant } = VIEWS[view];
  return (
    <Badge variant={variant} className={className} data-status={view} data-testid="worker-status">
      <Icon className={cn(view === "busy" && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
      {t(view)}
    </Badge>
  );
}

/** A short name in a bordered chip: a runtime, a project, a label. */
export function Chip({ children, mono = true }: { children: ReactNode; mono?: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center rounded-md border px-1.5 py-px text-xs text-muted-foreground [overflow-wrap:anywhere]",
        mono && "font-mono",
      )}
    >
      {children}
    </span>
  );
}

/** Chips in a list; `compact` sets the empty text at the chips' size, for table cells. */
export function ChipList({
  items,
  empty,
  mono = true,
  compact = false,
  label,
}: {
  items: string[];
  empty: string;
  mono?: boolean;
  compact?: boolean;
  label?: string;
}) {
  if (items.length === 0) return <span className={cn("text-muted-foreground", compact ? "text-xs" : "text-sm")}>{empty}</span>;
  return (
    <ul className="flex flex-wrap gap-1" aria-label={label}>
      {items.map((item) => (
        <li key={item} className="max-w-full">
          <Chip mono={mono}>{item}</Chip>
        </li>
      ))}
    </ul>
  );
}
