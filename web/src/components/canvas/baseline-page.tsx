"use client";

import { CalendarRange, Lock } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type CanvasBaseline, baselineQuery } from "./api";
import { BaselineStatusBadge, ClaimCode, ClaimStatusBadge } from "./badges";
import { BaselineItem } from "./baseline-item";
import { ReaderNote, SectionCard } from "./chrome";
import { useCanvasViewer } from "./hooks";
import { canvasHref, claimHref } from "./links";
import { canWrite } from "./model";

/** The snapshot an approved baseline froze: each claim at its rev and the status in effect, with its content's hash. */
function Snapshot({ project, slug, baseline }: { project: string; slug: string; baseline: CanvasBaseline }) {
  const t = useTranslations("canvas.snapshot");
  if (!baseline.snapshot) return null;
  return (
    <SectionCard
      title={
        <span className="inline-flex items-center gap-2">
          <Lock className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("heading", { count: baseline.snapshot.length })}
        </span>
      }
      headingId="baseline-snapshot-title"
      testId="baseline-snapshot"
    >
      <p className="text-[13px] text-muted-foreground">{t("text")}</p>
      <ul className="flex min-w-0 flex-col gap-2">
        {baseline.snapshot.map((claim) => (
          <li key={claim.claim_id} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 border-t pt-2 text-[13px] first:border-t-0 first:pt-0" data-claim={claim.claim}>
            <ClaimCode code={claim.claim} href={claimHref(project, slug, claim.claim)} />
            <span className="text-xs text-muted-foreground tabular-nums">{t("rev", { rev: claim.rev })}</span>
            <ClaimStatusBadge status={claim.status} />
            <span className="ml-auto truncate font-mono text-xs text-fg-subtle" title={claim.sha256}>
              {claim.sha256.slice(0, 12)}
            </span>
          </li>
        ))}
      </ul>
    </SectionCard>
  );
}

/** One line under the head: the canvas, when it was proposed and by what, and when it was approved or expired. */
function BaselineSub({ project, slug, baseline }: { project: string; slug: string; baseline: CanvasBaseline }) {
  const t = useTranslations("canvas.baseline");
  const link = "text-brand underline underline-offset-4 transition-colors hover:text-brand-hover";
  return (
    <>
      {t("ofCanvas")}{" "}
      <Link href={canvasHref(project, slug)} className={link}>
        {baseline.canvas}
      </Link>
      . {baseline.kind === "push" ? t("openedBy", { login: baseline.created_by ?? "" }) : t("proposed")} <When value={baseline.created_at} />.
      {baseline.approved_at ? (
        <>
          {" "}
          {t("approvedBy", { logins: baseline.approved_by.join(", ") || t("nobody") })} <When value={baseline.approved_at} />.
        </>
      ) : null}
      {baseline.expired_at ? (
        <>
          {" "}
          {t("expiredAt")} <When value={baseline.expired_at} />.
        </>
      ) : null}
    </>
  );
}

function BaselineView({ project, slug, baseline }: { project: string; slug: string; baseline: CanvasBaseline }) {
  const t = useTranslations("canvas.baseline");
  const format = useFormatter();
  /** When the baseline was proposed for, in its own time zone; in UTC when this browser does not know that zone. */
  const dueIn = (at: string, zone: string | null) => {
    try {
      return format.dateTime(new Date(at), { dateStyle: "medium", timeStyle: "short", timeZone: zone ?? "UTC" });
    } catch {
      return format.dateTime(new Date(at), { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" });
    }
  };
  const viewer = useCanvasViewer(project);
  const writer = canWrite(viewer);
  const open = baseline.status === "proposed";
  const items = [...baseline.items].sort((a, b) => Number(a.decision !== null) - Number(b.decision !== null) || a.claim.localeCompare(b.claim, undefined, { numeric: true }));
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="baseline-page" data-baseline={baseline.number} data-status={baseline.status}>
      <PageHeader
        title={t("title", { number: baseline.number })}
        status={<BaselineStatusBadge status={baseline.status} size="lg" />}
        tags={
          <Tag>
            <CalendarRange aria-hidden="true" />
            {baseline.kind === "weekly" ? t("weekly", { week: baseline.week ?? "" }) : t("push")}
          </Tag>
        }
        sub={<BaselineSub project={project} slug={slug} baseline={baseline} />}
        testId="baseline-head"
      />
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      {open ? (
        <Banner
          icon={CalendarRange}
          tone={baseline.waiting > 0 ? "attention" : "neutral"}
          title={t("waiting", { count: baseline.waiting, total: baseline.item_count })}
          testId="baseline-progress"
        >
          {baseline.due_at ? t("due", { when: dueIn(baseline.due_at, baseline.time_zone), zone: baseline.time_zone ?? "UTC" }) : t("approveWhen")}
        </Banner>
      ) : null}
      {baseline.status === "expired" ? (
        <Banner icon={CalendarRange} title={t("expiredTitle")} testId="baseline-expired">
          {t("expiredText")}
        </Banner>
      ) : null}
      <section aria-labelledby="baseline-items-title" className="flex min-w-0 flex-col gap-3">
        <h2 id="baseline-items-title" className="text-[15px] leading-[22px] font-semibold">
          {t("items", { count: baseline.items.length })}
        </h2>
        <ul className="flex min-w-0 flex-col gap-3">
          {items.map((item) => (
            <BaselineItem key={item.claim} project={project} slug={slug} number={baseline.number} item={item} open={open} viewer={viewer?.login ?? null} />
          ))}
        </ul>
      </section>
      <Snapshot project={project} slug={slug} baseline={baseline} />
    </div>
  );
}

/**
 * A baseline's review (/p/{project}/x/canvas/canvases/{slug}/baselines/{n}), the page the notice canvas_baseline links:
 * each claim whose status would take effect, with its evidence, for a writer to accept, reject or defer; each test card
 * past its deadline without real evidence, to extend with a new deadline or retire. Whoever changed a claim's status,
 * or opened the baseline, may not decide it. Once no item waits the baseline is approved, a snapshot that never changes.
 */
export function BaselinePage({ project, slug, number, initialError }: { project: string; slug: string; number: number; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(baselineQuery(browserApi, project, slug, number), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(baseline) => <BaselineView project={project} slug={slug} baseline={baseline} />}
    </QueryView>
  );
}
