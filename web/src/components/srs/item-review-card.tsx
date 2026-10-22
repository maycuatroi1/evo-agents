"use client";

import { ClipboardCheck } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import type { SrsUseCase, VersionState } from "./api";
import { CaseStateBadge, ChangeBadge } from "./badges";
import { reviewHref } from "./links";

/**
 * Where a use case stands in the review of the version the item page shows: what the version does to it, whether it is
 * pending, approved or asked for changes, who reviewed it, and the link to the review page where a writer reviews it.
 * Only a version that is a draft or in review has use cases to review, so any other version shows no card.
 */
export function ItemReviewCard({
  project,
  slug,
  version,
  state,
  useCase,
}: {
  project: string;
  slug: string;
  version: number;
  state: VersionState | null;
  useCase: SrsUseCase;
}) {
  const t = useTranslations("srs.itemPage.review");
  const tReview = useTranslations("srs.review");
  const inReview = state === "in_review";
  return (
    <Card size="sm" data-testid="srs-item-review" data-state={useCase.state}>
      <CardHeader>
        <CardTitle>
          <h2>{t("title", { version })}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-[13px]">
        <p className="flex flex-wrap items-center gap-2">
          <ChangeBadge change={useCase.change} />
          {inReview ? <CaseStateBadge state={useCase.state} /> : null}
        </p>
        <p className="text-muted-foreground">{inReview ? t(`state.${useCase.state}`, { version }) : t("draft", { version })}</p>
        {inReview && useCase.reviews.length > 0 ? (
          <ul className="flex flex-col gap-1" role="list" aria-label={t("reviewers")}>
            {useCase.reviews.map((review) => (
              <li key={`${review.reviewer}-${review.created_at}`} data-decision={review.decision}>
                <span className="font-medium">{review.reviewer}</span>{" "}
                <span className="text-muted-foreground">{tReview(`decisions.${review.decision}`)}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {inReview ? (
          <Link href={reviewHref(project, slug, version)} className="inline-flex items-center gap-1.5 text-brand hover:text-brand-hover">
            <ClipboardCheck className="size-4" aria-hidden="true" />
            {t("open", { version })}
          </Link>
        ) : null}
      </CardContent>
    </Card>
  );
}
