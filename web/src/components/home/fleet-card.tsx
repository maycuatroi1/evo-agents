"use client";

import { useQuery } from "@tanstack/react-query";
import { Server } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { NAME_LINK } from "@/components/data/identifier";
import { LoadingState } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { HeartbeatBars } from "@/components/workers/heartbeat-strip";
import { readRuntimes, workerView } from "@/components/workers/model";
import { type Worker, workerHref, WORKERS_HREF, workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { myWorkers } from "./model";
import { CARD_LINK, HomeCard, Relative } from "./parts";

/**
 * Home's Fleet card: the visitor's own workers from the list the Workers page reads (every 10 seconds), each with its
 * state, the last hour of heartbeats as the kit's compact strip, what it runs and when it was last heard from.
 */

function FleetWorker({ worker }: { worker: Worker }) {
  const t = useTranslations("home.fleet");
  const view = workerView(worker);
  const runtimes = readRuntimes(worker.runtimes)
    .filter((runtime) => runtime.available !== false)
    .map((runtime) => runtime.name);
  return (
    <li className="flex min-w-0 flex-col gap-1.5 border-t px-4 py-3 first:border-t-0" data-testid="fleet-worker" data-worker-id={worker.id} data-status={view}>
      <div className="flex min-w-0 items-center gap-2">
        <Link href={workerHref(worker.id)} className={cn(NAME_LINK, "min-w-0 truncate text-sm")} title={worker.name}>
          {worker.name}
        </Link>
        <StatusBadge
          kind="worker"
          status={view}
          label={view === "busy" ? t("busy", { held: worker.held_runs, slots: worker.slots }) : undefined}
          className="ml-auto"
        />
      </div>
      <HeartbeatBars workerId={worker.id} createdAt={worker.created_at} />
      <p className="text-xs leading-4 text-fg-subtle [overflow-wrap:anywhere]">
        {t("line", { runtimes: runtimes.length > 0 ? runtimes.join(", ") : t("noRuntime"), os: worker.os, arch: worker.arch })},{" "}
        {worker.last_heartbeat_at ? t.rich("heartbeat", { time: () => <Relative value={worker.last_heartbeat_at!} /> }) : t("never")}
      </p>
    </li>
  );
}

export function Fleet({ viewer }: { viewer: string | null }) {
  const t = useTranslations("home.fleet");
  const workers = useQuery(workersQuery(browserApi));
  const mine = workers.data ? myWorkers(workers.data, viewer) : null;
  let body: ReactNode;
  if (mine === null && workers.isError) {
    body = (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-4 text-[13px] text-danger" data-testid="fleet-error">
        <span>{t("error")}</span>
        <Button type="button" variant="outline" size="sm" onClick={() => void workers.refetch()}>
          {t("retry")}
        </Button>
      </div>
    );
  } else if (mine === null) {
    body = (
      <LoadingState className="flex flex-col gap-3 px-4 py-3">
        {[0, 1].map((index) => (
          <div key={index} className="flex flex-col gap-1.5">
            <Skeleton className="h-4 w-40 rounded-xs" />
            <Skeleton className="h-[18px] w-full max-w-[298px] rounded-xs" />
            <Skeleton className="h-3 w-48 rounded-xs" />
          </div>
        ))}
      </LoadingState>
    );
  } else if (mine.length === 0) {
    body = (
      <div className="flex flex-col items-start gap-2 px-4 py-4" data-testid="fleet-empty">
        <p className="text-[13px] leading-[18px] font-medium">{t("emptyTitle")}</p>
        <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground">{t("emptyText")}</p>
        <Button asChild variant="outline" size="sm">
          <Link href={WORKERS_HREF}>
            <Server aria-hidden="true" />
            {t("register")}
          </Link>
        </Button>
      </div>
    );
  } else {
    body = (
      <ul>
        {mine.map((worker) => (
          <FleetWorker key={worker.id} worker={worker} />
        ))}
      </ul>
    );
  }
  return (
    <HomeCard
      title={t("title")}
      action={
        <Link href={WORKERS_HREF} className={CARD_LINK} data-testid="fleet-all">
          {t("all")}
        </Link>
      }
      testId="fleet"
    >
      {body}
    </HomeCard>
  );
}
