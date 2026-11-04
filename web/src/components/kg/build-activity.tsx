"use client";

import { CircleAlert, Hammer, Hourglass } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Ago } from "@/components/data/ago";
import { Banner } from "@/components/feedback/banner";
import { buildDurations } from "@/lib/kg/format";
import type { KgBuild } from "@/lib/kg/types";

import { BuildStatusBadge, useDurationText } from "./badges";
import { latestSucceeded } from "./build-status";

/** What the page says above its figures: the build in flight, else the newest build when it failed, else nothing. */
export type Activity =
  | { kind: "running" | "queued"; build: KgBuild; waiting: number }
  | { kind: "failed"; build: KgBuild }
  | null;

export function buildActivity(builds: readonly KgBuild[]): Activity {
  const running = builds.find((build) => build.status === "running");
  // Newest first: the oldest queued build is the next to run.
  const queued = builds.filter((build) => build.status === "queued");
  if (running) return { kind: "running", build: running, waiting: queued.length };
  const next = queued.at(-1);
  if (next) return { kind: "queued", build: next, waiting: queued.length - 1 };
  const [newest] = builds;
  return newest?.status === "failed" ? { kind: "failed", build: newest } : null;
}

/**
 * One banner, only when the builds need a word: the build running or waiting for a worker (how long, who queued it,
 * how many wait after it), or the newest build when it failed, with its error and the graph the pages still read.
 */
export function BuildActivity({ builds, now }: { builds: readonly KgBuild[]; now: number | null }) {
  const t = useTranslations("kg.activity");
  const text = useDurationText();
  const activity = buildActivity(builds);
  if (!activity) return null;
  const { build } = activity;
  const { wait, run } = buildDurations(build, now);
  const title = (
    <span className="inline-flex flex-wrap items-center gap-2">
      <span className="tabular-nums">{t("build", { id: build.id })}</span>
      <BuildStatusBadge status={build.status} />
    </span>
  );
  const queuedBy = (
    <>
      {build.requested_by ? t("requestedBy", { login: build.requested_by }) : t("byPush")}{" "}
      <Ago value={build.queued_at} never="-" />
    </>
  );
  let banner: ReactNode;
  if (activity.kind === "failed") {
    const graph = latestSucceeded(builds);
    banner = (
      <Banner tone="danger" icon={CircleAlert} title={title}>
        <div className="flex flex-col gap-1.5">
          {build.error ? (
            <p className="font-mono text-xs whitespace-pre-wrap text-danger [overflow-wrap:anywhere]" data-testid="build-error">
              {build.error}
            </p>
          ) : null}
          <p>
            {t("failedAt")} <Ago value={build.finished_at} never="-" />.{" "}
            {graph ? t("stillReads", { id: graph.id }) : t("noGraphYet")}
          </p>
        </div>
      </Banner>
    );
  } else {
    const more = activity.waiting > 0 ? ` ${t("moreWaiting", { count: activity.waiting })}` : "";
    banner = (
      <Banner tone="neutral" icon={activity.kind === "running" ? Hammer : Hourglass} title={title}>
        <p>
          {activity.kind === "running"
            ? t("running", { run: run === null ? "-" : text(run), wait: wait === null ? "-" : text(wait) })
            : t("queued", { wait: wait === null ? "-" : text(wait) })}{" "}
          {queuedBy}.{more}
        </p>
      </Banner>
    );
  }
  return (
    <section aria-label={t("label")} data-testid="kg-activity" data-build-id={build.id} data-status={build.status}>
      {banner}
    </section>
  );
}
