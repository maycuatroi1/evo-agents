"use client";

import { ClipboardCheck } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { Button } from "@/components/ui/button";

import type { CanvasBaselineSummary } from "./api";
import { BaselineStatusBadge } from "./badges";
import { SectionCard } from "./chrome";
import { baselineHref } from "./links";

/** The baselines that wait for review, as a banner over the canvas: the first one opens, the count says the rest. */
export function WaitingBaselines({ project, slug, baselines }: { project: string; slug: string; baselines: readonly CanvasBaselineSummary[] }) {
  const t = useTranslations("canvas.baselines");
  const waiting = baselines.filter((baseline) => baseline.status === "proposed" && baseline.waiting > 0);
  if (waiting.length === 0) return null;
  const [first] = waiting;
  return (
    <Banner
      icon={ClipboardCheck}
      tone="attention"
      title={t("waitingTitle", { number: first.number, count: first.waiting })}
      actions={
        <Button asChild size="sm" variant="outline">
          <Link href={baselineHref(project, slug, first.number)} data-testid="baseline-review-open">
            {t("review")}
          </Link>
        </Button>
      }
      testId="canvas-baseline-waiting"
    >
      {waiting.length > 1 ? t("waitingMore", { count: waiting.length - 1 }) : t("waitingText")}
    </Banner>
  );
}

/** A baseline as a line of the list: its number linking to its review, its week or who opened it, its state, when. */
function BaselineLine({ project, slug, baseline }: { project: string; slug: string; baseline: CanvasBaselineSummary }) {
  const t = useTranslations("canvas.baselines");
  const what = baseline.kind === "weekly" ? t("weekly", { week: baseline.week ?? "" }) : t("push", { login: baseline.created_by ?? "" });
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 border-t pt-2.5 text-[13px] first:border-t-0 first:pt-0" data-testid="baseline-line" data-baseline={baseline.number}>
      <Link href={baselineHref(project, slug, baseline.number)} className="font-medium text-foreground transition-colors hover:text-brand">
        {t("name", { number: baseline.number })}
      </Link>
      <BaselineStatusBadge status={baseline.status} />
      <span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]">{what}</span>
      <span className="text-xs text-muted-foreground tabular-nums">
        {baseline.status === "proposed" ? t("waitingCount", { count: baseline.waiting, total: baseline.item_count }) : t("items", { count: baseline.item_count })}
      </span>
      <span className="ml-auto text-xs text-fg-subtle">
        <When value={baseline.approved_at ?? baseline.expired_at ?? baseline.created_at} />
      </span>
    </li>
  );
}

/**
 * The baselines of a canvas, the newest first: the weekly ones the plugin proposes and those a writer opened for one
 * claim at once. An approved baseline is a snapshot of the status in effect of every claim, which never changes.
 */
export function BaselinesCard({ project, slug, baselines }: { project: string; slug: string; baselines: readonly CanvasBaselineSummary[] }) {
  const t = useTranslations("canvas.baselines");
  const shown = [...baselines].sort((a, b) => b.number - a.number);
  return (
    <SectionCard title={t("heading")} headingId="canvas-baselines-title" testId="canvas-baselines">
      {shown.length === 0 ? (
        <p className="text-[13px] text-fg-subtle" data-testid="canvas-baselines-empty">
          {t("none")}
        </p>
      ) : (
        <ul className="flex min-w-0 flex-col gap-2.5">
          {shown.map((baseline) => (
            <BaselineLine key={baseline.number} project={project} slug={slug} baseline={baseline} />
          ))}
        </ul>
      )}
    </SectionCard>
  );
}
