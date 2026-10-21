"use client";

import { useQuery } from "@tanstack/react-query";
import { ChartNoAxesColumn, GitMerge, SlidersHorizontal } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { TabNav } from "@/components/data/tab-nav";
import { PageHeader } from "@/components/shell/page-header";
import { browserApi } from "@/lib/api/browser";
import { mergeQueueHref } from "@/lib/merge-queue";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

export type MergeTab = "queue" | "figures" | "policy";

/** The signed-in member's role on `project`, from whoami; null until whoami is read. */
export function useMergeViewer(project: string): { login: string; role: string | null; admin: boolean } | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return { login: data.login, role, admin: role === "admin" };
}

/**
 * The head of the merge queue's pages and their tabs: the queue, its figures and the autonomy policy that decides what
 * the hub merges on its own. `waiting` counts what waits for an admin (tripped breakers, samples to answer).
 */
export function MergeQueueHeader({
  project,
  current,
  waiting = 0,
  actions,
  sub,
}: {
  project: string;
  current: MergeTab;
  waiting?: number;
  actions?: ReactNode;
  sub?: ReactNode;
}) {
  const t = useTranslations("mergeQueue");
  const tabs = [
    {
      id: "queue",
      label: t("tabs.queue"),
      href: mergeQueueHref(project),
      icon: GitMerge,
      count: waiting || null,
      countLabel: t("tabs.waiting", { count: waiting }),
    },
    { id: "figures", label: t("tabs.figures"), href: mergeQueueHref(project, "figures"), icon: ChartNoAxesColumn },
    { id: "policy", label: t("tabs.policy"), href: mergeQueueHref(project, "policy"), icon: SlidersHorizontal },
  ];
  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t("title")} actions={actions} sub={sub} testId="merge-queue-header" />
      <TabNav label={t("tabs.label")} tabs={tabs} current={current} testId="merge-queue-tabs" tabTestId="merge-queue-tab" />
    </div>
  );
}

/** A card with a 48 px head (its h2, a count said in words, an action on the right) over its content. */
export function Panel({
  title,
  count,
  countWords,
  tone = "neutral",
  action,
  children,
  className,
  testId,
}: {
  title: string;
  count?: number;
  countWords?: string;
  tone?: "neutral" | "attention";
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  testId: string;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className={cn("min-w-0 rounded-md border bg-card shadow-raised", className)} data-testid={testId}>
      <div className="flex min-h-12 flex-wrap items-center gap-x-2 gap-y-1 border-b px-4 py-2">
        <h2 id={headingId} className="text-[15px] leading-[22px] font-semibold">
          {title}
        </h2>
        {count ? (
          <>
            <span
              className={cn(
                "inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-medium tabular-nums",
                tone === "attention" ? "bg-attention-soft text-attention" : "bg-muted text-muted-foreground",
              )}
              aria-hidden="true"
              data-testid={`${testId}-count`}
            >
              {count}
            </span>
            {countWords ? <span className="sr-only">{countWords}</span> : null}
          </>
        ) : null}
        {action ? <div className="ml-auto flex items-center gap-2">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** A link inside a line of text: `brand` with a quiet underline, so colour is not all that sets it apart. */
export const TEXT_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";
