"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { Identifier } from "@/components/data/identifier";
import { signalsHref } from "@/lib/signal-queries";
import { cn } from "@/lib/utils";

import type { CanvasClaimDetail } from "./api";
import { Fact, SectionCard } from "./chrome";
import { FEEDBACK, isOverdue, utcToday } from "./model";

const LINK = "text-brand underline underline-offset-4 transition-colors hover:text-brand-hover";

/** How the value check compares the metric's signals with the threshold, in one line ("mean over 30 days >= 0.4"). */
function useCheckLine(): (claim: CanvasClaimDetail) => string | null {
  const t = useTranslations("canvas.card");
  const format = useFormatter();
  return (claim) => {
    const card = claim.test_card;
    if (!card?.metric || !card.comparator || card.threshold === null) return null;
    const threshold = format.number(card.threshold);
    const days = card.window_days ?? 90;
    if (card.metric === FEEDBACK) return t("checkFeedback", { comparator: card.comparator, threshold, days });
    return t("checkNumber", { aggregate: t(`aggregates.${card.aggregate ?? "last"}`), comparator: card.comparator, threshold, days });
  };
}

/**
 * A claim's test card as facts: the hypothesis and the test, the metric linking to its signals (of the card's tenant),
 * how the value check reads it, at least how many samples, and the deadline, in amber once it passed without real
 * evidence. A claim without a test card says so; testing needs one.
 */
export function TestCardFacts({ project, claim }: { project: string; claim: CanvasClaimDetail }) {
  const t = useTranslations("canvas.card");
  const format = useFormatter();
  const check = useCheckLine();
  const card = claim.test_card;
  const filled = card && Object.values(card).some((value) => value !== null);
  const overdue = isOverdue(claim, utcToday());
  return (
    <SectionCard title={t("heading")} headingId="claim-card-title" testId="claim-test-card">
      {!filled || !card ? (
        <p className="text-[13px] text-fg-subtle">{t("empty")}</p>
      ) : (
        <dl className="grid min-w-0 gap-x-6 gap-y-3 sm:grid-cols-2">
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
          {check(claim) ? <Fact term={t("check")}>{check(claim)}</Fact> : null}
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
      )}
    </SectionCard>
  );
}

/** The use cases that realize a claim and the signal keys that measure it, each key opening its signals. */
export function ClaimLinks({ project, claim }: { project: string; claim: CanvasClaimDetail }) {
  const t = useTranslations("canvas.links");
  return (
    <SectionCard title={t("heading")} headingId="claim-links-title" testId="claim-links">
      <dl className="grid min-w-0 gap-x-6 gap-y-3 sm:grid-cols-2">
        <Fact term={t("useCases")}>
          {claim.use_cases.length === 0 ? (
            <span className="text-fg-subtle">{t("none")}</span>
          ) : (
            <span className="flex flex-wrap gap-1">
              {claim.use_cases.map((code) => (
                <Identifier key={code} value={code} data-code={code} />
              ))}
            </span>
          )}
        </Fact>
        <Fact term={t("signalKeys")}>
          {claim.signal_keys.length === 0 ? (
            <span className="text-fg-subtle">{t("none")}</span>
          ) : (
            <span className="flex flex-wrap gap-1">
              {claim.signal_keys.map((key) => (
                <Identifier key={key} value={key} href={signalsHref(project, key)} data-key={key} />
              ))}
            </span>
          )}
        </Fact>
      </dl>
    </SectionCard>
  );
}
