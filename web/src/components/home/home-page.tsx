"use client";

import { useQuery } from "@tanstack/react-query";
import { FolderGit2, Shield } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Identifier } from "@/components/data/identifier";
import type { DecisionTarget } from "@/components/inbox/decision-sheet";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { overviewQuery, whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { Fleet } from "./fleet-card";
import { HomeMetrics } from "./home-metrics";
import { homeRefreshInterval, type Overview, type OverviewDecision } from "./model";
import { AuthorWaiting } from "./author-waiting";
import { LazyDecisionSheet, NeedsYou } from "./needs-you";
import { Projects } from "./projects-card";
import { InFlight, Recent } from "./run-lists";

/** A member without a grant: who grants roles, and the login to send them; a hub admin goes to Administration. */
function NoGrants() {
  const t = useTranslations("home.empty");
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const login = me?.login ?? "";
  return (
    <EmptyState
      icon={FolderGit2}
      title={t("title")}
      description={me?.admin ? t("adminDescription") : t("description", { login })}
    >
      {me?.admin ? (
        <Button asChild>
          <Link href={"/admin/members" as Route}>
            <Shield aria-hidden="true" />
            {t("admin")}
          </Link>
        </Button>
      ) : login ? (
        <Identifier value={login} copy copyLabel={t("copyLogin")} testId="home-login" />
      ) : null}
    </EmptyState>
  );
}

function HomeSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border bg-border shadow-raised lg:grid-cols-5">
        {Array.from({ length: 5 }, (_, index) => (
          <div key={index} className={cn("flex h-[78px] flex-col justify-between bg-card px-4 py-3", index === 4 && "max-lg:col-span-2")}>
            <Skeleton className="h-3 w-20 rounded-xs" />
            <Skeleton className="h-6 w-10 rounded-xs" />
            <Skeleton className="h-2.5 w-28 rounded-xs" />
          </div>
        ))}
      </div>
      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_300px] xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex flex-col gap-5">
          {[3, 2].map((rows) => (
            <div key={rows} className="overflow-hidden rounded-md border bg-card shadow-raised">
              <div className="flex h-12 items-center border-b px-4">
                <Skeleton className="h-3.5 w-24 rounded-xs" />
              </div>
              {Array.from({ length: rows }, (_, index) => (
                <div key={index} className="flex items-center gap-3 border-t px-4 py-3 first:border-t-0">
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
        <Skeleton className="h-48 w-full rounded-md" />
      </div>
    </div>
  );
}

function MissionControl({ overview, viewer }: { overview: Overview; viewer: string | null }) {
  const [target, setTarget] = useState<DecisionTarget | null>(null);
  const [sheetUsed, setSheetUsed] = useState(false);
  const answer = (decision: OverviewDecision) => {
    setSheetUsed(true);
    setTarget({ id: decision.id, project: decision.project, parksAt: decision.parks_at });
  };
  return (
    <div className="flex flex-col gap-6">
      <HomeMetrics overview={overview} />
      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_300px] xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex min-w-0 flex-col gap-5">
          <NeedsYou overview={overview} onAnswer={answer} />
          <AuthorWaiting overview={overview} />
          <InFlight overview={overview} viewer={viewer} />
          <Recent overview={overview} viewer={viewer} />
        </div>
        <div className="grid min-w-0 items-start gap-5 md:max-lg:grid-cols-2">
          <Fleet viewer={viewer} />
          <Projects projects={overview.projects} />
        </div>
      </div>
      {sheetUsed ? (
        <LazyDecisionSheet
          target={target}
          onClose={() => setTarget(null)}
          returnFocus={(id) => document.querySelector<HTMLElement>(`[data-decision-link="${id}"]`) ?? document.getElementById("main")}
        />
      ) : null}
    </div>
  );
}

/**
 * Home (`/`), the kit's MissionControl (web/DESIGN.md, Home): what needs the visitor, what is in flight, what ended
 * lately, their workers and their projects, from one read of GET /v1/me/overview (and the workers list for the Fleet
 * card). The overview is asked every 5 seconds while a run is in flight or a decision is open, every 30 otherwise, and
 * the top bar says whether the page is current. Under 768 px it is one column, Needs you first.
 */
export function HomePage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("home");
  const { data: me } = useQuery(whoamiQuery(browserApi));
  // The page's main query: the top bar says from it whether Home is current.
  const state = useHubQuery({ ...overviewQuery(browserApi), refetchInterval: homeRefreshInterval }, initialError, { live: true });
  return (
    <>
      <PageHeader title={t("title")} />
      <QueryView state={state} loading={<HomeSkeleton />}>
        {(overview) =>
          overview.projects.length === 0 ? <NoGrants /> : <MissionControl overview={overview} viewer={me?.login ?? null} />
        }
      </QueryView>
    </>
  );
}
