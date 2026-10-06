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
import { useEffect, useState } from "react";

import { Tag as TagChip } from "@/components/data/identifier";
import { useVisibilityName } from "@/components/data/visibility";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type DurationParts, durationParts } from "@/lib/kg/format";
import { kindStyle, SHAPE_POINTS, type Tone } from "@/lib/kg/graph";
import type { BuildStatus, KgJob, NodeLabel } from "@/lib/kg/types";
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

export function JobStatusBadge({ status }: { status: KgJob["status"] }) {
  const t = useTranslations("kg.queue");
  return status === "doing" ? (
    <Badge variant="info" data-status="doing">
      <LoaderCircle aria-hidden="true" className="motion-safe:animate-spin" />
      {t("doing")}
    </Badge>
  ) : (
    <Badge variant="secondary" data-status="todo">
      <Clock aria-hidden="true" />
      {t("todo")}
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

const TONE_FILL: Record<Tone, string> = {
  1: "fill-chart-1",
  2: "fill-chart-2",
  3: "fill-chart-3",
  4: "fill-chart-4",
  5: "fill-chart-5",
};

/** The outline a kind of node is drawn with in the graph view, in its colour. Decoration: the kind is in text. */
export function KindShape({ kind, className }: { kind: string; className?: string }) {
  const { tone, shape } = kindStyle(kind);
  const fill = cn(TONE_FILL[tone], "stroke-card");
  return (
    <svg viewBox="-1.2 -1.2 2.4 2.4" aria-hidden="true" className={cn("size-3.5 shrink-0", className)}>
      {shape === "ellipse" ? (
        <circle r="1" className={fill} strokeWidth="0.15" />
      ) : shape === "rectangle" || shape === "round-rectangle" ? (
        <rect x="-1" y="-0.8" width="2" height="1.6" rx={shape === "round-rectangle" ? 0.4 : 0} className={fill} strokeWidth="0.15" />
      ) : (
        <polygon points={SHAPE_POINTS[shape]} className={fill} strokeWidth="0.15" />
      )}
    </svg>
  );
}

export function KindBadge({ kind }: { kind: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium whitespace-nowrap" data-testid="kind">
      <KindShape kind={kind} />
      {kind}
    </span>
  );
}

/** A duration in words, from `kg.duration`. */
export function Duration({ ms }: { ms: number | null }) {
  const t = useTranslations("kg.duration");
  if (ms === null) return <span className="text-muted-foreground">-</span>;
  const parts: DurationParts = durationParts(ms);
  const text =
    parts.unit === "underSecond"
      ? t("underSecond")
      : parts.unit === "seconds"
        ? t("seconds", { seconds: parts.seconds })
        : parts.unit === "minutes"
          ? t("minutes", { minutes: parts.minutes, seconds: parts.seconds })
          : t("hours", { hours: parts.hours, minutes: parts.minutes });
  return <span className="tabular-nums">{text}</span>;
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

/** Copies `value` and says so in a live region; the button keeps its name for screen readers. */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const t = useTranslations("kg");
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2_000);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={label}
        title={label}
        className="cursor-pointer text-muted-foreground"
        onClick={() => {
          void navigator.clipboard?.writeText(value).then(() => setCopied(true), () => setCopied(false));
        }}
      >
        {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      </Button>
      <span className="sr-only" aria-live="polite">
        {copied ? t("copied") : ""}
      </span>
    </>
  );
}
