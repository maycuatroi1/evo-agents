"use client";

import { ArrowRight, CircleAlert, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import type { Handoff } from "@/lib/systems-work";

/**
 * The hand-offs of one work, in the order made (web/DESIGN.md, Observe: work and timeline): who handed the turn to
 * whom and when, an ask the hub refused in `danger` with its reason, and one off the design marked in `attention`;
 * the heading counts them and those off the design.
 */
export function WorkHandoffs({ handoffs, nameOf }: { handoffs: readonly Handoff[]; nameOf: (node: string) => string }) {
  const t = useTranslations("systems.observe.work.handoffs");
  const format = useFormatter();
  const off = handoffs.filter((handoff) => handoff.offDesign).length;
  return (
    <section aria-labelledby="system-handoffs-title" className="flex min-w-0 flex-col gap-2" data-testid="system-handoffs" data-off-design={off}>
      <h3 id="system-handoffs-title" className="flex flex-wrap items-baseline gap-x-2 text-sm font-semibold">
        {t("title")}
        <span className="text-xs font-normal text-muted-foreground tabular-nums">{t("count", { count: handoffs.length })}</span>
        {off ? (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-attention tabular-nums" data-testid="system-handoffs-off">
            <TriangleAlert className="size-3.5" aria-hidden="true" />
            {t("offDesign", { count: off })}
          </span>
        ) : null}
      </h3>
      {handoffs.length === 0 ? (
        <p className="text-[13px] text-pretty text-muted-foreground">{t("empty")}</p>
      ) : (
        <ol className="flex flex-col divide-y rounded-md border">
          {handoffs.map((handoff) => (
            <li key={handoff.id} className="flex flex-col gap-0.5 px-3 py-2 text-[13px]" data-testid="system-handoff" data-from={handoff.from} data-to={handoff.to} data-refused={String(handoff.refused)} data-off-design={String(handoff.offDesign)}>
              <span className="flex min-w-0 items-center gap-1.5">
                <span className="sr-only">{t("route", { from: nameOf(handoff.from), to: nameOf(handoff.to) })}</span>
                <span className="truncate font-medium" aria-hidden="true">
                  {nameOf(handoff.from)}
                </span>
                <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="truncate font-medium" aria-hidden="true">
                  {nameOf(handoff.to)}
                </span>
                <time className="ml-auto shrink-0 text-xs text-fg-subtle tabular-nums" dateTime={handoff.at}>
                  {format.dateTime(new Date(handoff.at), { timeStyle: "medium" })}
                </time>
              </span>
              {handoff.refused || handoff.offDesign ? (
                <span className="flex flex-wrap items-center gap-x-2 text-xs">
                  {handoff.refused ? (
                    <span className="inline-flex items-center gap-1 text-danger">
                      <CircleAlert className="size-3.5" aria-hidden="true" />
                      {t("refused")}
                    </span>
                  ) : null}
                  {handoff.offDesign ? <span className="text-attention">{t("offDesignTag")}</span> : null}
                  {handoff.reason ? <span className="text-pretty text-muted-foreground">{handoff.reason}</span> : null}
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
