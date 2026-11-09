"use client";

import { BookOpenCheck, Send } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";

import { type SrsItem, type SrsUseCase, type SrsVersion, submitUseCase } from "./api";
import { CaseStateBadge, CodeChip } from "./badges";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { caseHref } from "./links";

/** What a use case's line on the contents says and offers, worked out from its item, its review and the versions. */
export type UseCaseLine = {
  label: string; // UC ORD-1 r3
  state: SrsUseCase["state"] | null; // where its review stands in the version shown, when it is to review there
  approved: number | null; // the revision the approved baseline holds
  review: boolean; // it waits for a review the viewer can open
  submit: boolean; // a writer may send it to review on its own
};

export function useCaseLine(
  item: Pick<SrsItem, "code" | "title" | "rev" | "released" | "key">,
  useCase: Pick<SrsUseCase, "state" | "submitted"> | null,
  shown: Pick<SrsVersion, "number" | "state">,
  latest: Pick<SrsVersion, "number" | "state"> | null,
  writer: boolean,
): UseCaseLine {
  const name = item.code ?? item.title;
  const waiting = useCase !== null && (useCase.state === "pending" || useCase.state === "changes_requested");
  const reviewable = waiting && (shown.state === "in_review" || useCase.submitted);
  const isLatest = latest !== null && latest.number === shown.number;
  const fresh = Boolean(item.rev) && item.released !== item.rev;
  return {
    label: item.rev ? `${name} r${item.rev}` : name,
    state: useCase?.state ?? null,
    approved: item.released ?? null,
    review: reviewable,
    submit: writer && isLatest && latest?.state === "draft" && fresh && !useCase?.submitted,
  };
}

/**
 * A use case's line at the head of its contents: its code and revision, where its review stands (approved, waiting,
 * changes asked, sent on its own), a link to its review when it waits for one, and for a writer Send this use case to
 * review when the latest version, a draft, holds a revision of it not approved yet.
 */
export function UseCaseStatus({
  project,
  slug,
  item,
  useCase,
  shown,
  latest,
  writer,
  onReview = false,
}: {
  project: string;
  slug: string;
  item: SrsItem;
  useCase: SrsUseCase | null;
  shown: SrsVersion;
  latest: SrsVersion | null;
  writer: boolean;
  onReview?: boolean; // shown on the use case's own review: no link to it
}) {
  const t = useTranslations("srs.useCaseLine");
  const refresh = useDocumentRefresh(project, slug);
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const line = useCaseLine(item, useCase, shown, latest, writer);
  const send = async () => {
    setBusy(true);
    try {
      await submitUseCase(browserApi(), project, slug, item.code ?? item.key);
      await refresh();
      notify({ tone: "success", text: t("sent", { name: line.label }) });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("sendFailed"), failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/30 px-3 py-2" data-testid="srs-use-case-line">
      <CodeChip code={line.label} testId="srs-use-case-rev" />
      {line.state ? <CaseStateBadge state={line.state} /> : null}
      {useCase?.submitted ? <span className="text-xs text-muted-foreground">{t("sentAlone")}</span> : null}
      <span className="text-xs text-muted-foreground" data-testid="srs-use-case-approved">
        {line.approved ? t("approvedRev", { rev: line.approved }) : t("notApproved")}
      </span>
      <span className="ml-auto flex flex-wrap gap-2">
        {line.review && !onReview ? (
          <Button asChild size="sm">
            <Link href={caseHref(project, slug, shown.number, item.key)} data-testid="srs-use-case-review">
              <BookOpenCheck aria-hidden="true" />
              {t("review")}
            </Link>
          </Button>
        ) : null}
        {line.submit ? (
          <Button type="button" variant="secondary" size="sm" busy={busy} onClick={() => void send()} data-testid="srs-use-case-submit">
            <Send aria-hidden="true" />
            {t("send")}
          </Button>
        ) : null}
      </span>
    </div>
  );
}
