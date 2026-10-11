"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import { Identifier } from "@/components/data/identifier";
import { cn } from "@/lib/utils";

import { isActiveState, type Run, runHref } from "./queries";
import { readStepSessions } from "./sessions-model";

/**
 * The sessions of a plan run with a session for each step, a card of the run's side column: a row per session with its
 * step, the agent session's id (to copy, for a session to go on with by hand), the largest context it read and its
 * cache reads, and the run that ran it when that is another one (a parked run this one resumes). The worker reports
 * the list with the run's end; until then the card says so, and the log notes each session as it starts.
 */
export function RunSessions({ run }: { run: Pick<Run, "id" | "project" | "state" | "usage"> }) {
  const t = useTranslations("runs.detail.stepSessions");
  const format = useFormatter();
  const id = useId();
  const read = readStepSessions(run.usage);
  const active = isActiveState(run.state);
  const tokens = (value: number) => format.number(value);
  const listed = read !== null && read.sessions.length > 0;
  const status = read ? t("count", { count: read.sessions.length + read.leftOut }) : active ? t("pending") : t("none");

  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={id} data-testid="run-step-sessions">
      <div className={cn("flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-3", (listed || (!read && active)) && "border-b")}>
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <span className="ml-auto text-xs text-fg-subtle" data-testid="run-step-sessions-status">
          {status}
        </span>
      </div>
      {!read && active ? <p className="px-4 py-3 text-xs text-pretty text-muted-foreground">{t("note")}</p> : null}
      {listed ? (
        <div className="flex min-w-0 flex-col gap-2 px-4 py-3">
          <ol className="flex flex-col divide-y text-sm">
            {read.sessions.map((session, index) => (
              <li key={`${index}-${session.step}`} className="flex min-w-0 flex-col gap-1 py-2 first:pt-0 last:pb-0" data-testid="run-step-session" data-step={session.step}>
                <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-medium">{t("step", { step: session.step })}</span>
                  {session.runId !== null && session.runId !== run.id ? (
                    <Link href={runHref(run.project, session.runId)} className="text-xs text-brand underline-offset-4 hover:underline" data-testid="run-step-session-run">
                      {t("inRun", { id: session.runId })}
                    </Link>
                  ) : null}
                  <span className="ml-auto min-w-0">
                    {session.sessionId ? (
                      <Identifier value={session.sessionId} copy copyLabel={t("copy", { step: session.step })} />
                    ) : (
                      <span className="text-xs text-muted-foreground">{t("noSession")}</span>
                    )}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground tabular-nums">
                  {session.peak !== null ? t("peak", { tokens: tokens(session.peak) }) : t("peakUnknown")}
                  {", "}
                  {t("cacheRead", { tokens: tokens(session.cacheRead) })}
                </p>
              </li>
            ))}
          </ol>
          {read.leftOut > 0 ? <p className="text-xs text-fg-subtle">{t("leftOut", { count: read.leftOut })}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
