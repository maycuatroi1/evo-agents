"use client";

import {
  Check,
  CircleCheck,
  CircleX,
  Clock,
  Copy,
  LoaderCircle,
  type LucideIcon,
  Tag,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { Tag as TagChip, useClipboard } from "@/components/data/identifier";
import { useVisibilityName } from "@/components/data/visibility";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type DurationParts, durationParts } from "@/lib/kg/format";
import { kindTone, type Tone } from "@/lib/kg/graph";
import type { BuildStatus, NodeLabel } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

/** Each state as an icon and a word, so colour is never the only signal. */
const BUILD_STATUS: Record<BuildStatus, { icon: LucideIcon; variant: "secondary" | "info" | "success" | "destructive" }> = {
  queued: { icon: Clock, variant: "secondary" },
  running: { icon: LoaderCircle, variant: "info" },
  succeeded: { icon: CircleCheck, variant: "success" },
  failed: { icon: CircleX, variant: "destructive" },
};

export function BuildStatusBadge({ status }: { status: BuildStatus }) {
  const t = useTranslations("kg.status");
  const { icon: Icon, variant } = BUILD_STATUS[status];
  return (
    <Badge variant={variant} data-status={status} data-testid="build-status">
      <Icon aria-hidden="true" className={cn(status === "running" && "motion-safe:animate-spin")} />
      {t(status)}
    </Badge>
  );
}

/**
 * A node's label as a tag: its visibility in words (Internal, the code in the tooltip with the location and the
 * integrity), and the location when it says more than "any".
 */
export function LabelBadge({ label }: { label: NodeLabel }) {
  const t = useTranslations("kg.node");
  const visibility = useVisibilityName();
  const level = visibility(label.level);
  const text = label.location && label.location !== "any" ? `${level} / ${label.location}` : level;
  return (
    <TagChip
      title={t("labelTitle", { level: label.level, location: label.location, integrity: label.integrity })}
      data-level={label.level}
      data-testid="label-badge"
    >
      <Tag aria-hidden="true" />
      <span className="sr-only">{t("label")}: </span>
      {text}
    </TagChip>
  );
}

const TONE_BG: Record<Tone, string> = {
  1: "bg-chart-1",
  2: "bg-chart-2",
  3: "bg-chart-3",
  4: "bg-chart-4",
  5: "bg-chart-5",
};

/** The disc a kind of node is drawn with on every canvas of the graph, in its colour. Decoration: the kind is in text. */
export function KindDot({ kind, className }: { kind: string; className?: string }) {
  return <span aria-hidden="true" className={cn("size-2.5 shrink-0 rounded-full", TONE_BG[kindTone(kind)], className)} data-tone={kindTone(kind)} />;
}

export function KindBadge({ kind }: { kind: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium whitespace-nowrap" data-testid="kind">
      <KindDot kind={kind} />
      {kind}
    </span>
  );
}

/** A duration in words, from `kg.duration`. */
/** A duration in words ("4 min 32 s"), for a figure that takes a string. */
export function useDurationText() {
  const t = useTranslations("kg.duration");
  return (ms: number) => {
    const parts: DurationParts = durationParts(ms);
    return parts.unit === "underSecond"
      ? t("underSecond")
      : parts.unit === "seconds"
        ? t("seconds", { seconds: parts.seconds })
        : parts.unit === "minutes"
          ? t("minutes", { minutes: parts.minutes, seconds: parts.seconds })
          : t("hours", { hours: parts.hours, minutes: parts.minutes });
  };
}

export function Duration({ ms }: { ms: number | null }) {
  const text = useDurationText();
  if (ms === null) return <span className="text-muted-foreground">-</span>;
  return <span className="tabular-nums">{text(ms)}</span>;
}

/**
 * A date and time, with the full value in the markup for machines and on hover. Given `now` (the browser's clock,
 * see `useNow`), it reads as relative time; without it, as a date, which the server and the browser render alike.
 */
export function When({ iso, now = null }: { iso: string | null | undefined; now?: number | null }) {
  const format = useFormatter();
  if (!iso) return <span className="text-muted-foreground">-</span>;
  const date = new Date(iso);
  const full = format.dateTime(date, { dateStyle: "medium", timeStyle: "medium" });
  return (
    <time dateTime={iso} title={full} className="tabular-nums">
      {now !== null ? format.relativeTime(date, now) : format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
    </time>
  );
}

/** Copies `value` and says so in a toast; the button keeps its name for screen readers. */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const t = useTranslations("kg");
  const { state, copy } = useClipboard(value, null, { copied: t("copied") });
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={label}
        title={label}
        className="cursor-pointer text-muted-foreground"
        onClick={() => void copy()}
      >
        {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      </Button>
    </>
  );
}
