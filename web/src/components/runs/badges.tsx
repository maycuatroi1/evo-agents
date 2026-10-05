"use client";

import {
  Ban,
  CircleCheck,
  CircleX,
  Clock,
  Eye,
  Hand,
  ListChecks,
  Loader2,
  type LucideIcon,
  SquareTerminal,
  TimerOff,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

import type { RunState } from "./queries";

type Variant = "success" | "info" | "warning" | "destructive" | "secondary";

/** Each state's icon and colour; the word is always there too, so colour is never the only signal. */
export const STATE_LOOK: Record<RunState, { icon: LucideIcon; variant: Variant; spin?: boolean }> = {
  queued: { icon: Clock, variant: "warning" },
  leased: { icon: Hand, variant: "info" },
  running: { icon: Loader2, variant: "info", spin: true },
  interactive: { icon: SquareTerminal, variant: "warning" },
  verifying: { icon: ListChecks, variant: "info" },
  review: { icon: Eye, variant: "warning" },
  done: { icon: CircleCheck, variant: "success" },
  failed: { icon: CircleX, variant: "destructive" },
  lost: { icon: TimerOff, variant: "secondary" },
  cancelled: { icon: Ban, variant: "secondary" },
};

export function RunStateBadge({ state, className }: { state: RunState; className?: string }) {
  const t = useTranslations("runs.state");
  const { icon: Icon, variant, spin } = STATE_LOOK[state];
  return (
    <Badge variant={variant} className={className} data-state={state} data-testid="run-state">
      <Icon className={cn(spin && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
      {t(state)}
    </Badge>
  );
}
