"use client";

import { ClipboardCheck, PencilLine } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type CanvasClaimDetail, claimQuery, openBaseline } from "./api";
import { ClaimCode, ClaimStatusBadge, ConfidenceTag } from "./badges";
import { ReaderNote } from "./chrome";
import { ClaimEditor } from "./claim-editor";
import { ClaimLinks, TestCardFacts } from "./claim-facts";
import { ClaimHistory } from "./claim-history";
import { EvidencePane } from "./evidence-pane";
import { signInAgainOn401, useCanvasRefresh, useCanvasViewer } from "./hooks";
import { JudgingsCard } from "./judgings";
import { baselineHref, canvasHref } from "./links";
import { canWrite } from "./model";

/** Open a baseline of this claim at once, for another writer to decide; the toast links to its review. */
function AskReview({ project, slug, code }: { project: string; slug: string; code: string }) {
  const t = useTranslations("canvas.claim");
  const failure = useWriteFailure();
  const refresh = useCanvasRefresh(project);
  const [busy, setBusy] = useState(false);
  const ask = async () => {
    setBusy(true);
    try {
      const opened = await openBaseline(browserApi(), project, slug, code);
      notify({
        tone: "success",
        text: t("reviewOpened", { number: opened.number }),
        description: t("reviewOpenedText"),
        link: { label: t("openReview"), href: baselineHref(project, slug, opened.number) },
      });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("reviewFailed", { claim: code }), failure(error, { 409: t("reviewConflict") }));
    } finally {
      setBusy(false);
      await refresh(slug);
    }
  };
  return (
    <Button type="button" variant="outline" busy={busy} onClick={() => void ask()} data-testid="claim-ask-review">
      <ClipboardCheck aria-hidden="true" />
      {t("askReview")}
    </Button>
  );
}

function ClaimView({ project, slug, claim }: { project: string; slug: string; claim: CanvasClaimDetail }) {
  const t = useTranslations("canvas.claim");
  const tCell = useTranslations("canvas.cell");
  const viewer = useCanvasViewer(project);
  const writer = canWrite(viewer);
  const [editing, setEditing] = useState(false);
  const link = "text-brand underline underline-offset-4 transition-colors hover:text-brand-hover";
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="claim-page" data-claim={claim.code} data-rev={claim.rev}>
      <PageHeader
        leading={<ClaimCode code={claim.code} testId="claim-page-code" />}
        title={claim.text}
        status={<ClaimStatusBadge status={claim.status} size="lg" />}
        tags={
          <>
            <Tag data-cell={claim.cell}>{tCell(claim.cell)}</Tag>
            <ConfidenceTag confidence={claim.confidence} />
            {claim.archived ? <Tag data-testid="claim-archived">{t("archived")}</Tag> : null}
          </>
        }
        sub={
          <>
            {t("inCanvas")}{" "}
            <Link href={canvasHref(project, slug)} className={link} data-testid="claim-canvas-link">
              {claim.canvas}
            </Link>
            . {t("created", { login: claim.created_by })} <When value={claim.created_at} />. {t("atRev", { rev: claim.rev })}
          </>
        }
        actions={
          writer && !editing ? (
            <>
              <AskReview project={project} slug={slug} code={claim.code} />
              <Button type="button" onClick={() => setEditing(true)} data-testid="claim-edit">
                <PencilLine aria-hidden="true" />
                {t("edit")}
              </Button>
            </>
          ) : null
        }
        testId="claim-head"
      />
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          {editing ? <ClaimEditor project={project} slug={slug} claim={claim} onClose={() => setEditing(false)} /> : null}
          <EvidencePane project={project} slug={slug} claim={claim} writer={writer} />
          <JudgingsCard project={project} slug={slug} code={claim.code} />
          <ClaimHistory project={project} slug={slug} claim={claim} writer={writer} />
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <TestCardFacts project={project} claim={claim} />
          <ClaimLinks project={project} claim={claim} />
        </div>
      </div>
    </div>
  );
}

/**
 * A claim whole (/p/{project}/x/canvas/canvases/{slug}/claims/{code}): its head with its status, cell and confidence;
 * its evidence pane, the verdicts of @strategist's judging runs and its history of revs with what each changed, and
 * beside them its test card and links. A writer edits it at the rev the page read, adds and withdraws evidence, undoes
 * the current rev and asks for its review at once; a reader reads.
 */
export function ClaimPage({ project, slug, code, initialError }: { project: string; slug: string; code: string; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(claimQuery(browserApi, project, slug, code), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(claim) => <ClaimView project={project} slug={slug} claim={claim} />}
    </QueryView>
  );
}
