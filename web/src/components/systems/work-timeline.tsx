"use client";

import { useFormatter, useTranslations } from "next-intl";

import { agentName, type BarKind, barsOf, lanesOf, percentAt, scaleOf, ticksOf, type TimelineMark, type TraceTimeline } from "@/lib/systems-work";
import { cn } from "@/lib/utils";

import { useWorkDuration } from "./work-status";

/**
 * Each kind of bar on the timeline: colour only for state (web/DESIGN.md, Colour), and a shape that tells them apart
 * without it: queued an empty dashed outline, parked a soft fill inside a solid edge, waiting a neutral fill.
 */
const BAR_LOOK: Record<BarKind, string> = {
  queued: "border border-dashed border-neutral-solid bg-transparent",
  running: "bg-running",
  done: "bg-success-solid",
  failed: "bg-danger-solid",
  parked: "border border-attention-solid bg-attention-soft",
  waiting: "bg-neutral-solid",
};
const LEGEND: readonly BarKind[] = ["queued", "running", "done", "failed", "parked", "waiting"];

/** A decision that asks a person: a mark in `attention-solid` from the moment it was asked. */
function Mark({ mark, left, label }: { mark: TimelineMark; left: number; label: string }) {
  return (
    <li className="absolute inset-y-0 w-0" style={{ left: `${left}%` }} data-testid="system-timeline-mark" data-decision={mark.decision_id} data-state={mark.state}>
      <span className="absolute inset-y-[-3px] left-[-1px] w-0.5 bg-attention-solid" aria-hidden="true" />
      <span className="absolute top-[-6px] left-[-4px] size-2 rotate-45 bg-attention-solid ring-2 ring-card" aria-hidden="true" title={label} />
      <span className="sr-only">{label}</span>
    </li>
  );
}

function Key() {
  const t = useTranslations("systems.observe.work.timeline");
  return (
    <ul aria-label={t("legend")} className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted-foreground" data-testid="system-timeline-legend">
      {LEGEND.map((kind) => (
        <li key={kind} className="inline-flex items-center gap-1.5">
          <span className={cn("h-2.5 w-4 rounded-xs", BAR_LOOK[kind])} aria-hidden="true" />
          {t(`kinds.${kind}`)}
        </li>
      ))}
      <li className="inline-flex items-center gap-1.5">
        <span className="size-2 rotate-45 bg-attention-solid" aria-hidden="true" />
        {t("kinds.needsPerson")}
      </li>
    </ul>
  );
}

/**
 * The timeline of one work by agent (web/DESIGN.md, Observe: work and timeline): a lane per agent with a bar for each
 * stretch of its runs (queued, running, then done or failed once the run ended so, parked, waiting for an answer), a
 * mark where a decision asked a person, the time since the work started under it and the key. Each bar says what it is
 * and when to a screen reader. A bar still going runs on to now.
 */
export function WorkTimeline({ timeline, now, names }: { timeline: TraceTimeline; now: number; names: ReadonlyMap<string, string> }) {
  const t = useTranslations("systems.observe.work.timeline");
  const format = useFormatter();
  const duration = useWorkDuration();
  const scale = scaleOf(timeline, now);
  const lanes = lanesOf(timeline);
  const at = (iso: string) => format.dateTime(new Date(iso), { timeStyle: "medium" });
  return (
    <section aria-labelledby="system-timeline-title" className="flex min-w-0 flex-col gap-3" data-testid="system-timeline">
      <h3 id="system-timeline-title" className="text-sm font-semibold">
        {t("title")}
      </h3>
      {scale === null || lanes.length === 0 ? (
        <p className="text-[13px] text-pretty text-muted-foreground" data-testid="system-timeline-empty">
          {t("empty")}
        </p>
      ) : (
        <>
          <ol className="flex flex-col gap-2">
            {lanes.map((lane) => {
              const name = agentName(lane.agent, names);
              const runs = new Set(lane.runs.map((run) => run.run_id));
              const marks = (timeline.marks ?? []).filter((mark) => mark.agent === lane.agent || runs.has(mark.run_id));
              return (
                <li key={lane.agent} className="grid grid-cols-[minmax(0,6.5rem)_1fr] items-center gap-3 sm:grid-cols-[minmax(0,10rem)_1fr]" data-testid="system-timeline-lane" data-agent={lane.agent}>
                  <span className="truncate text-[13px] font-medium" title={name}>
                    {name}
                  </span>
                  <ul aria-label={t("lane", { agent: name })} className="relative h-5 rounded-sm bg-muted">
                    {lane.runs.flatMap((run) =>
                      barsOf(run, scale, now).map((bar, index) => {
                        const label = bar.endedAt ? t("bar", { kind: t(`kinds.${bar.kind}`), start: at(bar.startedAt), end: at(bar.endedAt) }) : t("barOpen", { kind: t(`kinds.${bar.kind}`), start: at(bar.startedAt) });
                        return (
                          <li
                            key={`${run.run_id}-${index}`}
                            className={cn("absolute inset-y-1 rounded-xs", BAR_LOOK[bar.kind], bar.open && bar.kind === "running" && "motion-safe:animate-pulse")}
                            style={{ left: `${bar.left}%`, width: `${bar.width}%` }}
                            title={label}
                            data-testid="system-timeline-bar"
                            data-kind={bar.kind}
                            data-run={run.run_id}
                          >
                            <span className="sr-only">{label}</span>
                          </li>
                        );
                      }),
                    )}
                    {marks.map((mark) => {
                      const who = mark.login ? `@${mark.login}` : t("someone");
                      const label = mark.answered_at ? t("markAnswered", { login: who, question: mark.question }) : t("mark", { login: who, question: mark.question });
                      return <Mark key={mark.decision_id} mark={mark} left={percentAt(scale, Date.parse(mark.asked_at))} label={label} />;
                    })}
                  </ul>
                </li>
              );
            })}
          </ol>
          <div className="grid grid-cols-[minmax(0,6.5rem)_1fr] gap-3 sm:grid-cols-[minmax(0,10rem)_1fr]" aria-hidden="true">
            <span />
            <div className="relative h-4 border-t text-[11px] text-fg-subtle tabular-nums" data-testid="system-timeline-axis">
              {ticksOf(scale).map((tick, index, all) => (
                <span
                  key={tick}
                  className={cn("absolute top-0.5", index === 0 ? "left-0" : index === all.length - 1 ? "right-0" : "-translate-x-1/2", index % 2 === 1 && "max-sm:hidden")}
                  style={index === 0 || index === all.length - 1 ? undefined : { left: `${percentAt(scale, tick)}%` }}
                >
                  {duration((tick - scale.start) / 1000)}
                </span>
              ))}
            </div>
          </div>
          <p className="sr-only">{t("axis")}</p>
          <Key />
        </>
      )}
    </section>
  );
}
