"use client";

import { CircleCheck, CircleDot, CircleX } from "lucide-react";
import { useTranslations } from "next-intl";

import { Identifier, RunRef } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { projectHref } from "@/components/shell/nav";
import { EVENT_KINDS, type EventKind, type MergeEvent } from "@/lib/merge-queue";
import { cn } from "@/lib/utils";

import { shortSha } from "./model";

function isEventKind(kind: string): kind is EventKind {
  return (EVENT_KINDS as readonly string[]).includes(kind);
}

/** Passed, failed, or a line that is neither (queued, rebased): an icon in its tone and its word for screen readers. */
function Outcome({ passed }: { passed: boolean | null }) {
  const t = useTranslations("mergeQueue.ledger");
  if (passed === null) {
    return (
      <span className="mt-0.5 inline-flex shrink-0 text-fg-subtle" title={t("noOutcome")}>
        <CircleDot className="size-4" aria-hidden="true" />
      </span>
    );
  }
  return (
    <span className={cn("mt-0.5 inline-flex shrink-0", passed ? "text-success" : "text-danger")} title={passed ? t("passed") : t("failed")}>
      {passed ? <CircleCheck className="size-4" aria-hidden="true" /> : <CircleX className="size-4" aria-hidden="true" />}
      <span className="sr-only">{passed ? t("passed") : t("failed")}</span>
    </span>
  );
}

/**
 * A merge request's ledger, the newest line first, as the hub keeps it: each gate passed or not with why, each verdict,
 * the turn (rebased, pipeline, merged) and what follows the merge (checks, deploy, rollback, revert, sample).
 */
export function Ledger({ project, events }: { project: string; events: readonly MergeEvent[] }) {
  const t = useTranslations("mergeQueue.ledger");
  if (events.length === 0) return <p className="text-[13px] text-muted-foreground">{t("empty")}</p>;
  return (
    <ol className="flex flex-col" data-testid="merge-ledger">
      {events.map((event) => (
        <li
          key={event.id}
          className="flex min-w-0 gap-2.5 border-t py-2.5 first:border-t-0"
          data-testid="merge-event"
          data-kind={event.kind}
          data-passed={event.passed === null ? undefined : String(event.passed)}
        >
          <Outcome passed={event.passed} />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
              <span className="font-medium">
                {event.kind === "gate" && event.gate !== null
                  ? t("gate", { gate: event.gate, name: event.gate_name ?? "" })
                  : isEventKind(event.kind)
                    ? t(`kind.${event.kind}`)
                    : event.kind}
              </span>
              {event.sha ? (
                <Identifier value={event.sha} title={event.sha} testId="merge-event-sha">
                  {shortSha(event.sha)}
                </Identifier>
              ) : null}
              {event.run_id !== null ? (
                <RunRef id={event.run_id} href={projectHref(project, `runs/${event.run_id}`)} label={t("openRun", { id: event.run_id })} />
              ) : null}
              <span className="ml-auto text-xs text-fg-subtle">
                <When value={event.created_at} short />
              </span>
            </div>
            {event.reason ? <p className="text-xs text-pretty text-muted-foreground [overflow-wrap:anywhere]">{event.reason}</p> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}
