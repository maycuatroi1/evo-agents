"use client";

import { useTranslations } from "next-intl";

import { Identifier } from "@/components/data/identifier";
import { ProblemChips } from "@/components/data/problem-chips";
import { signalsHref } from "@/lib/signal-queries";

import type { CanvasClaimDetail } from "./api";
import { Fact, SectionCard } from "./chrome";
import { isOverdue, utcToday } from "./model";
import { TestCardList } from "./test-card-list";

/** A claim's test card as facts in its own card; a claim without one says so, since testing needs one. */
export function TestCardFacts({ project, claim }: { project: string; claim: CanvasClaimDetail }) {
  const t = useTranslations("canvas.card");
  const card = claim.test_card;
  const filled = card && Object.values(card).some((value) => value !== null);
  return (
    <SectionCard title={t("heading")} headingId="claim-card-title" testId="claim-test-card">
      {!filled || !card ? (
        <p className="text-[13px] text-fg-subtle">{t("empty")}</p>
      ) : (
        <TestCardList project={project} card={card} overdue={isOverdue(claim, utcToday())} />
      )}
    </SectionCard>
  );
}

/** The problems a claim addresses, the use cases that realize it and the signal keys that measure it, each opening its page. */
export function ClaimLinks({ project, claim }: { project: string; claim: CanvasClaimDetail }) {
  const t = useTranslations("canvas.links");
  return (
    <SectionCard title={t("heading")} headingId="claim-links-title" testId="claim-links">
      <dl className="grid min-w-0 gap-x-6 gap-y-3 sm:grid-cols-2">
        <Fact term={t("problems")} testId="claim-problems">
          {claim.problems.length === 0 ? <span className="text-fg-subtle">{t("none")}</span> : <ProblemChips project={project} codes={claim.problems} />}
        </Fact>
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
