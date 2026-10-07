"use client";

import {
  CalendarClock,
  ChevronRight,
  CircleCheck,
  CircleX,
  FolderKanban,
  HardDrive,
  Hourglass,
  KeyRound,
  type LucideIcon,
  ScrollText,
  Server,
  ServerOff,
  Trash2,
  Users,
} from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { type Metric, MetricStrip } from "@/components/data/metric-strip";
import { CARD_LINK, HomeCard } from "@/components/home/parts";
import { PageHeader } from "@/components/shell/page-header";
import { sizeParts } from "@/components/skills/format";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Skeleton } from "@/components/ui/skeleton";
import { Ago } from "@/components/workers/ago";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { type AdminOverview, adminOverviewQuery, type ProjectGrantCounts } from "./data";
import { type Attention, type AttentionTone, attentionItems, grantTotal, membersHref } from "./overview-model";

const TITLE = "line-clamp-2 text-sm leading-5 font-medium text-foreground [overflow-wrap:anywhere]";

const TONE_MARK: Record<AttentionTone, string> = {
  danger: "text-danger",
  attention: "text-attention",
  neutral: "text-muted-foreground",
};

/** A byte count as the overview reads it: "1.2 GB", "0 bytes". */
export function useByteSize() {
  const format = useFormatter();
  return (bytes: number) => {
    const { value, unit } = sizeParts(bytes);
    return format.number(value, {
      style: "unit",
      unit,
      unitDisplay: unit === "byte" ? "long" : "short",
      maximumFractionDigits: unit === "byte" ? 0 : 1,
    });
  };
}

/**
 * One row of a card's list that opens a page (the kit's `eh-li` as a link): a 20 px mark in its tone, the title over
 * one line of facts, and a chevron. Both are sentences that name a project or a count, so on a narrow screen each
 * wraps to a second line before it is cut. The title's link covers the row, so a click or tap anywhere opens the
 * page, and it is described by the facts. A row without a page has no link and no chevron.
 */
function LinkRow({
  icon: Icon,
  tone,
  title,
  meta,
  href,
  testId,
  data,
}: {
  icon: LucideIcon;
  tone: AttentionTone;
  title: ReactNode;
  meta: ReactNode;
  href: Route | null;
  testId: string;
  data?: Record<string, string | number>;
}) {
  const metaId = useId();
  return (
    <li
      className={cn(
        "relative grid min-h-[60px] grid-cols-[20px_minmax(0,1fr)_auto] items-center gap-x-3 border-t px-4 py-2.5 first:border-t-0",
        href && "transition-colors hover:bg-accent active:bg-accent",
      )}
      data-testid={testId}
      {...Object.fromEntries(Object.entries(data ?? {}).map(([key, value]) => [`data-${key}`, value]))}
    >
      <Icon className={cn("size-4 justify-self-center", TONE_MARK[tone])} aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-0.5">
        {href ? (
          <Link
            href={href}
            aria-describedby={metaId}
            className={cn(
              TITLE,
              "after:absolute after:inset-0 after:content-[''] focus-visible:outline-none",
              "focus-visible:after:rounded-xs focus-visible:after:outline-2 focus-visible:after:-outline-offset-2 focus-visible:after:outline-ring",
            )}
            data-testid={`${testId}-link`}
          >
            {title}
          </Link>
        ) : (
          <span className={TITLE}>{title}</span>
        )}
        <p id={metaId} className="line-clamp-2 text-xs leading-4 text-fg-subtle [overflow-wrap:anywhere]">
          {meta}
        </p>
      </div>
      {href ? <ChevronRight className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" /> : <span />}
    </li>
  );
}

const ICONS: Record<Attention["kind"], LucideIcon> = {
  builds: CircleX,
  offline: ServerOff,
  expiring: CalendarClock,
  unused: KeyRound,
  notSignedIn: Hourglass,
  deletions: Trash2,
};

function AttentionRow({ item }: { item: Attention }) {
  const t = useTranslations("admin.overview.attention");
  const byteSize = useByteSize();
  let title: string;
  let meta: ReactNode;
  switch (item.kind) {
    case "builds": {
      const { builds } = item;
      title = t("builds.title", { count: builds.failed, project: builds.project, days: item.days });
      meta =
        builds.latest_status === "failed" ? (
          <>
            {t("builds.lastFailure", { id: builds.last_failed_id })} <Ago value={builds.last_failed_at} never="-" />
          </>
        ) : (
          t("builds.since", { id: builds.latest_id, status: builds.latest_status })
        );
      break;
    }
    case "offline":
      title = t("offline.title", { count: item.count });
      meta = t("offline.meta", { minutes: Math.round(item.seconds / 60) });
      break;
    case "expiring":
      title = t("expiring.title", { count: item.count, days: item.days });
      meta = t("expiring.meta");
      break;
    case "unused":
      title = t("unused.title", { count: item.count, days: item.days });
      meta = t("unused.meta");
      break;
    case "notSignedIn":
      title = t("notSignedIn.title", { count: item.count });
      meta = t("notSignedIn.meta");
      break;
    case "deletions":
      title = t("deletions.title", { count: item.count });
      meta = t("deletions.meta", { size: byteSize(item.bytes) });
      break;
  }
  return (
    <LinkRow
      icon={ICONS[item.kind]}
      tone={item.tone}
      title={title}
      meta={meta}
      href={item.href}
      testId="attention-item"
      data={{ kind: item.kind, tone: item.tone, ...(item.kind === "builds" ? { project: item.builds.project } : {}) }}
    />
  );
}

/** What may need an admin, each row opening the page that lists it filtered; a quiet line when nothing does. */
function NeedsAttention({ overview }: { overview: AdminOverview }) {
  const t = useTranslations("admin.overview.attention");
  const items = attentionItems(overview);
  const pressing = items.filter((item) => item.tone !== "neutral").length;
  return (
    <HomeCard
      title={t("title")}
      count={{ value: pressing, tone: "attention", words: t("count", { count: pressing }) }}
      testId="admin-attention"
    >
      {items.length > 0 ? (
        <ul role="list" aria-label={t("title")}>
          {items.map((item) => (
            <AttentionRow key={item.kind === "builds" ? `builds-${item.builds.project}` : item.kind} item={item} />
          ))}
        </ul>
      ) : (
        <p className="flex items-start gap-3 px-4 py-5 text-[13px] leading-[18px] text-muted-foreground" data-testid="admin-all-clear">
          <CircleCheck className="mt-px size-4 shrink-0 text-success" aria-hidden="true" />
          <span className="text-pretty">
            <span className="font-semibold text-foreground">{t("clearTitle")}</span>{" "}
            {t("clear", { expiring: overview.tokens.expiring_days, days: overview.kg_builds.days })}
          </span>
        </p>
      )}
    </HomeCard>
  );
}

function AccessRow({ grants }: { grants: ProjectGrantCounts }) {
  const t = useTranslations("admin.overview.access");
  const total = grantTotal(grants);
  const roles = [
    grants.admins > 0 ? t("admins", { count: grants.admins }) : null,
    grants.writers > 0 ? t("writers", { count: grants.writers }) : null,
    grants.readers > 0 ? t("readers", { count: grants.readers }) : null,
  ].filter((part): part is string => part !== null);
  return (
    <LinkRow
      icon={FolderKanban}
      tone="neutral"
      title={grants.project}
      meta={total > 0 ? roles.join(", ") : t("nobody")}
      href={membersHref(grants.project)}
      testId="access-project"
      data={{ project: grants.project, members: total }}
    />
  );
}

/** Every project with its grants by role, each opening the members list filtered to it. */
function AccessByProject({ grants }: { grants: ProjectGrantCounts[] }) {
  const t = useTranslations("admin.overview.access");
  return (
    <HomeCard
      title={t("title")}
      action={
        <Link href={membersHref()} className={CARD_LINK} data-testid="access-all-members">
          {t("allMembers")}
        </Link>
      }
      testId="admin-access"
    >
      {grants.length > 0 ? (
        <ul role="list" aria-label={t("title")}>
          {grants.map((item) => (
            <AccessRow key={item.project} grants={item} />
          ))}
        </ul>
      ) : (
        <p className="px-4 py-5 text-[13px] leading-[18px] text-pretty text-muted-foreground">
          {t.rich("noProjects", { code: (chunks) => <code className="font-mono text-xs">{chunks}</code> })}
        </p>
      )}
    </HomeCard>
  );
}

function Overview({ overview }: { overview: AdminOverview }) {
  const t = useTranslations("admin.overview");
  const format = useFormatter();
  const byteSize = useByteSize();
  const { members, tokens, workers, storage, audit } = overview;
  const metrics: Metric[] = [
    {
      id: "members",
      label: t("metrics.members"),
      icon: Users,
      value: members.total,
      display: format.number(members.total),
      meta: t("metrics.membersMeta", { count: members.active, days: members.active_days }),
      href: membersHref(),
    },
    {
      id: "tokens",
      label: t("metrics.tokens"),
      icon: KeyRound,
      value: tokens.live,
      display: format.number(tokens.live),
      meta: t("metrics.tokensMeta", { count: tokens.expiring, days: tokens.expiring_days }),
      href: "/admin/tokens" as Route,
    },
    {
      id: "workers",
      label: t("metrics.workers"),
      icon: Server,
      value: workers.live,
      display: format.number(workers.live),
      meta: t("metrics.workersMeta", { count: workers.offline, live: workers.live }),
      href: "/workers" as Route,
    },
    {
      id: "storage",
      label: t("metrics.storage"),
      icon: HardDrive,
      value: storage.bytes,
      display: byteSize(storage.bytes),
      meta: t("metrics.storageMeta", { count: storage.objects }),
      href: "/admin/diagnostics" as Route,
    },
    {
      id: "audit",
      label: t("metrics.audit"),
      icon: ScrollText,
      value: audit.rows,
      display: format.number(audit.rows),
      meta: t("metrics.auditMeta", { hours: audit.hours }),
      href: "/admin/audit" as Route,
    },
  ];
  return (
    <div className="flex flex-col gap-6" data-testid="admin-overview">
      <MetricStrip label={t("metrics.label")} metrics={metrics} testId="admin-summary" />
      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <NeedsAttention overview={overview} />
        <AccessByProject grants={overview.grants} />
      </div>
      <p className="text-[13px] leading-[18px] text-muted-foreground" data-testid="admin-diagnostics-note">
        {t.rich("diagnostics", {
          link: (chunks) => (
            <Link
              href={"/admin/diagnostics" as Route}
              className="rounded-xs font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:inline-block max-md:py-3"
              data-testid="admin-diagnostics-link"
            >
              {chunks}
            </Link>
          ),
        })}
      </p>
    </div>
  );
}

function OverviewSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border bg-border shadow-raised lg:grid-cols-5">
        {Array.from({ length: 5 }, (_, index) => (
          <div key={index} className={cn("flex h-[78px] flex-col justify-between bg-card px-4 py-3", index === 4 && "max-lg:col-span-2")}>
            <Skeleton className="h-3 w-20 rounded-xs" />
            <Skeleton className="h-6 w-12 rounded-xs" />
            <Skeleton className="h-2.5 w-28 rounded-xs" />
          </div>
        ))}
      </div>
      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        {[3, 3].map((rows, card) => (
          <div key={card} className="overflow-hidden rounded-md border bg-card shadow-raised">
            <div className="flex h-12 items-center border-b px-4">
              <Skeleton className="h-3.5 w-32 rounded-xs" />
            </div>
            {Array.from({ length: rows }, (_, index) => (
              <div key={index} className="flex min-h-[60px] items-center gap-3 border-t px-4 py-2.5 first:border-t-0">
                <Skeleton className="size-4 rounded-full" />
                <div className="flex flex-1 flex-col gap-1.5">
                  <Skeleton className="h-3 w-[60%] rounded-xs" />
                  <Skeleton className="h-2.5 w-[40%] rounded-xs" />
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * /admin, the hub's administration overview (web/DESIGN.md, Administration): the hub at a glance in one strip whose
 * cells open their pages, what may need an admin with the page that lists it filtered, and every project's access.
 * The row counts of every table are on /admin/diagnostics, linked at the foot.
 */
export function AdminOverviewPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin");
  const state = useHubQuery(adminOverviewQuery(browserApi), initialError);
  return (
    <>
      <PageHeader title={t("title")} />
      <QueryView state={state} loading={<OverviewSkeleton />}>
        {(overview) => <Overview overview={overview} />}
      </QueryView>
    </>
  );
}
