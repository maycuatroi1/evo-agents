import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export type NoteTone = "info" | "warning" | "danger" | "muted";

/**
 * One line of what a worker's page says above its cards: why it is revoked, draining or offline, that a hub admin is
 * looking, or what went wrong with its updates. Each tone is a state's text on its soft ground (web/DESIGN.md, States):
 * `warning` is amber, a person is needed; `danger` is red, something failed.
 */
export function Note({ tone, icon: Icon, children, testId }: { tone: NoteTone; icon: LucideIcon; children: ReactNode; testId?: string }) {
  return (
    <div
      className={cn(
        "flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-sm",
        tone === "info" && "bg-muted text-foreground",
        tone === "warning" && "border-attention/20 bg-attention-soft text-attention",
        tone === "danger" && "border-danger/30 bg-danger-soft text-danger",
        tone === "muted" && "bg-card text-muted-foreground",
      )}
      data-testid={testId}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 space-y-1 text-pretty">{children}</div>
    </div>
  );
}
