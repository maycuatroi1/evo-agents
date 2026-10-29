"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { signalsHref } from "@/lib/signal-queries";
import { cn } from "@/lib/utils";

import type { CanvasTestCard } from "./api";
import { Fact } from "./chrome";
import { FEEDBACK } from "./model";

const LINK = "text-brand underline underline-offset-4 transition-colors hover:text-brand-hover";

/** How the value check compares the metric's signals with the threshold, in one line ("mean over 30 days >= 0.4"). */
function useCheckLine(): (card: CanvasTestCard) => string | null {
  const t = useTranslations("canvas.card");
  const format = useFormatter();
  return (card) => {
    if (!card.metric || !card.comparator || card.threshold === null) return null;
    const threshold = format.number(card.threshold);
    const days = card.window_days ?? 90;
    if (card.metric === FEEDBACK) return t("checkFeedback", { comparator: card.comparator, threshold, days });
    return t("checkNumber", { aggregate: t(`aggregates.${card.aggregate ?? "last"}`), comparator: card.comparator, threshold, days });
  };
}

/**
 * A test card's facts: the hypothesis and the test, the metric linking to its signals (of the card's tenant), how the
 * value check reads it, at least how many samples, and the deadline, in amber when `overdue`. A claim's page and a
 * proposed new claim show it.
 */
export function TestCardList({ project, card, overdue = false, className }: { project: string; card: CanvasTestCard; overdue?: boolean; className?: string }) {
  const t = useTranslations("canvas.card");
  const format = useFormatter();
  const check = useCheckLine()(card);
  return (
    <dl className={cn("grid min-w-0 gap-x-6 gap-y-3 sm:grid-cols-2", className)}>
      {card.hypothesis ? <Fact term={t("hypothesis")}>{card.hypothesis}</Fact> : null}
      {card.test ? <Fact term={t("test")}>{card.test}</Fact> : null}
      {card.metric ? (
        <Fact term={t("metric")} testId="claim-metric">
          {card.metric === FEEDBACK ? (
            t("feedback")
          ) : (
            <Link href={signalsHref(project, card.metric, card.tenant)} className={cn(LINK, "font-mono text-xs")} data-testid="claim-metric-link">
              {card.metric}
            </Link>
          )}
        </Fact>
      ) : null}
      {check ? <Fact term={t("check")}>{check}</Fact> : null}
      {card.min_samples ? <Fact term={t("minSamples")}>{format.number(card.min_samples)}</Fact> : null}
      {card.tenant ? <Fact term={t("tenant")}>{card.tenant}</Fact> : null}
      {card.deadline ? (
        <Fact term={t("deadline")} testId="claim-card-deadline">
          <span className={cn(overdue && "font-medium text-attention")}>
            {format.dateTime(new Date(`${card.deadline}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" })}
            {overdue ? ` (${t("pastDeadline")})` : null}
          </span>
        </Fact>
      ) : null}
    </dl>
  );
}
