"use client";

import { FilePen } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { NAME_LINK, RunRef } from "@/components/data/identifier";
import { useNow } from "@/components/kg/use-now";
import { runChatHref } from "@/components/runs/queries";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { Overview, OverviewAuthorWait } from "./model";
import { HomeCard, Relative, Row, useDuration } from "./parts";

/** "waiting 6 min" while the run waits, "parked 3 hours ago" once it has. */
function WaitTiming({ run }: { run: OverviewAuthorWait }) {
  const t = useTranslations("home.authorWaiting");
  const duration = useDuration();
  const now = useNow(run.state === "waiting" && run.waiting_since !== null);
  if (run.state === "parked" && run.parked_at) {
    return <span>{t.rich("parked", { time: () => <Relative value={run.parked_at!} /> })}</span>;
  }
  if (!run.waiting_since || now === null) return null;
  return <span>{t("waiting", { duration: duration(Math.max(0, now - Date.parse(run.waiting_since))) })}</span>;
}

/**
 * Home's Waiting for your reply: the visitor's author runs whose agent asked something in the chat and waits for
 * their answer (the overview's `author_waiting`), the latest first, each with the agent's last message and Reply,
 * which opens the run's chat.
 */
export function AuthorWaiting({ overview }: { overview: Overview }) {
  const t = useTranslations("home.authorWaiting");
  const runs = overview.author_waiting ?? [];
  if (runs.length === 0) return null;
  return (
    <HomeCard title={t("title")} count={{ value: runs.length, tone: "attention", words: t("count", { count: runs.length }) }} testId="author-waiting">
      <ul>
        {runs.map((run) => {
          const href = runChatHref(run.project, run.id);
          const what = run.message ?? run.title ?? t("untitled");
          return (
            <Row
              key={`${run.project}/${run.id}`}
              testId="author-waiting-item"
              data={{ "run-id": run.id }}
              mark={<FilePen className="size-4 text-attention" aria-hidden="true" />}
              title={
                <Link href={href} className={cn(NAME_LINK, "min-w-0 truncate")} title={what} data-testid="author-waiting-link">
                  {what}
                </Link>
              }
              sub={
                <>
                  <span className="min-w-0 [overflow-wrap:anywhere]">
                    <RunRef id={run.id} className="mr-2 text-xs" />
                    {run.plan_id
                      ? t("wherePlan", { project: run.project, plan: run.plan_title ?? run.plan_id })
                      : t("whereNew", { project: run.project })}
                  </span>
                  <WaitTiming run={run} />
                </>
              }
              end={
                <Button asChild size="sm" variant="outline">
                  <Link href={href} aria-label={t("replyLabel", { id: run.id })} data-testid="author-waiting-reply">
                    {t("reply")}
                  </Link>
                </Button>
              }
            />
          );
        })}
      </ul>
    </HomeCard>
  );
}
