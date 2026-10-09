"use client";

import { CirclePlay, LayoutDashboard, Lightbulb, type LucideIcon, Pause, ScrollText } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { ConfirmAction } from "@/components/admin/confirm-action";
import type { WriteFailure } from "@/components/admin/notice";
import { Identifier } from "@/components/data/identifier";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Ago } from "@/components/workers/ago";
import { cn } from "@/lib/utils";

import { useCuratorFailure, useCuratorRights, usePauseCurator } from "./hooks";
import { curatorLook } from "./model";
import { type CuratorStatus, curatorHref } from "./queries";

type CuratorPage = "overview" | "proposals" | "charter";

const TABS: readonly { id: CuratorPage; icon: LucideIcon; page: "" | "proposals" | "charter" }[] = [
  { id: "overview", icon: LayoutDashboard, page: "" },
  { id: "proposals", icon: Lightbulb, page: "proposals" },
  { id: "charter", icon: ScrollText, page: "charter" },
];

/** The Curator's own pages as tabs under its head, the page shown underlined in `brand`, as the admin area's. */
export function CuratorTabs({ project, waiting }: { project: string; waiting: number }) {
  const t = useTranslations("curator.tabs");
  const pathname = usePathname();
  return (
    <nav aria-label={t("label")} className="-mt-2 border-b" data-testid="curator-tabs">
      <ul className="-mb-px flex gap-1 overflow-x-auto">
        {TABS.map((tab) => {
          const href = curatorHref(project, tab.page);
          const active = tab.page === "" ? pathname === href : pathname === href || pathname.startsWith(`${href}/`);
          return (
            <li key={tab.id} className="shrink-0">
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "inline-flex h-11 items-center gap-2 rounded-t-sm border-b-2 px-2.5 text-sm font-medium transition-colors sm:px-3",
                  active ? "border-brand text-foreground" : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                )}
                data-testid={`curator-tab-${tab.id}`}
              >
                <tab.icon className="hidden size-4 sm:block" aria-hidden="true" />
                {t(tab.id)}
                {tab.id === "proposals" && waiting > 0 ? (
                  <>
                    <span
                      aria-hidden="true"
                      className="inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-full bg-attention-soft px-1.25 text-[11px] leading-none font-medium text-attention tabular-nums"
                    >
                      {waiting > 99 ? "99+" : waiting}
                    </span>
                    <span className="sr-only">{t("waiting", { count: waiting })}</span>
                  </>
                ) : null}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/** Pause, or Resume while paused, for an admin or the owner of a schedule: a pause is confirmed first, a resume is not. */
function PauseControl({ project, status }: { project: string; status: CuratorStatus }) {
  const t = useTranslations("curator");
  const rights = useCuratorRights(project, status);
  const mutation = usePauseCurator(project);
  const failure = useCuratorFailure();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  if (!rights.pause || status.charter === null) return null;

  const run = (pause: boolean) => {
    setError(null);
    mutation.mutate(pause, {
      onSuccess: () => {
        setConfirming(false);
        notify({
          tone: "success",
          text: pause ? t("paused.title") : t("resumed.title"),
          description: pause ? t("paused.text") : t("resumed.text"),
        });
      },
      onError: (failed) => {
        const said = failure(failed, "pause");
        if (pause) setError(said);
        else notifyFailure(t("resumeFailed"), said);
      },
    });
  };

  if (status.paused) {
    return (
      <Button
        variant="default"
        busy={mutation.isPending}
        onClick={() => {
          if (!mutation.isPending) run(false);
        }}
        data-testid="curator-resume"
      >
        <CirclePlay aria-hidden="true" />
        {mutation.isPending ? t("resuming") : t("resume")}
      </Button>
    );
  }
  return (
    <>
      <Button variant="secondary" onClick={() => setConfirming(true)} data-testid="curator-pause">
        <Pause aria-hidden="true" />
        {t("pause")}
      </Button>
      <ConfirmAction
        open={confirming}
        onOpenChange={(open) => {
          setConfirming(open);
          if (!open) setError(null);
        }}
        title={t("confirmPause.title")}
        description={<p>{t("confirmPause.text", { project })}</p>}
        confirmLabel={t("confirmPause.confirm")}
        pendingLabel={t("pausing")}
        cancelLabel={t("confirmPause.cancel")}
        pending={mutation.isPending}
        error={error}
        onConfirm={() => run(true)}
        testId="curator-pause-dialog"
      />
    </>
  );
}

/**
 * The head of the Curator's pages: "Curator", where its night shift stands as a pill, Pause or Resume for those who
 * may, and one line under it: the worker on duty, the member it runs as and its window, or who paused it and when.
 */
export function CuratorHeader({ project, status, actions }: { project: string; status: CuratorStatus; actions?: ReactNode }) {
  const t = useTranslations("curator");
  const charter = status.charter;
  const pausedBy = status.schedules.find((schedule) => schedule.paused_at !== null) ?? null;
  const sub: ReactNode = charter
    ? status.paused && pausedBy
      ? pausedBy.paused_by === null
        ? t.rich("sub.pausedByHub", { ago: () => <Ago value={pausedBy.paused_at} never="" /> })
        : t.rich("sub.paused", {
            login: pausedBy.paused_by,
            ago: () => <Ago value={pausedBy.paused_at} never="" />,
          })
      : t.rich("sub.duty", {
          start: charter.window.start,
          end: charter.window.end,
          timezone: charter.window.timezone,
          owner: charter.schedule_owner,
          worker: () => <Identifier value={charter.worker} className="align-baseline" />,
        })
    : null;
  return (
    <PageHeader
      title={t("title")}
      status={<StatusBadge kind="curator" status={curatorLook(status)} size="lg" />}
      actions={
        <>
          {actions}
          <PauseControl project={project} status={status} />
        </>
      }
      sub={sub}
      testId="curator-header"
    />
  );
}
