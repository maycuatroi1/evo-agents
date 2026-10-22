"use client";

import { CircleCheck, Lock, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useRef, useState } from "react";

import { When } from "@/components/data/when";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { StatusPill } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { type ReviewRole, reviewUseCase, type SrsReviewIn, type SrsUseCase } from "./api";
import { CaseStateBadge, ChangeBadge, CodeChip, VersionChip } from "./badges";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { itemHref } from "./links";
import { carriedFrom, caseName, caseRef, caseVersion, ownReview } from "./use-case-model";

const MAX_REVIEW_CHARS = 10_000; // evo_agents.hub.tables_srs.MAX_REVIEW_CHARS

/** What the viewer may do on a use case now: nothing, or review it, with Approve locked while the hub would refuse it. */
export type CaseActions = { login: string; role: ReviewRole | ""; locked: boolean; lockId: string };

/** The reviews that count on a use case: who, for which role, what they said and, when it is older, the version. */
function CaseReviews({ useCase, version, login }: { useCase: SrsUseCase; version: number; login: string | null }) {
  const t = useTranslations("srs.review.cases");
  const tReview = useTranslations("srs.review");
  const tRole = useTranslations("srs.role");
  if (useCase.reviews.length === 0) return <p className="text-[13px] text-muted-foreground">{t("noReviews")}</p>;
  return (
    <ul className="flex flex-col divide-y rounded-md border" aria-label={t("reviewsLabel", { name: caseName(useCase) })} role="list">
      {useCase.reviews.map((review) => {
        const approve = review.decision === "approve";
        const from = carriedFrom(review, version);
        return (
          <li key={`${review.reviewer}-${review.created_at}`} className="flex flex-col gap-1 px-3 py-2 text-[13px]" data-testid="srs-case-review">
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <StatusPill
                look={approve ? { tone: "success", icon: CircleCheck } : { tone: "attention", icon: Undo2 }}
                label={tReview(`decisions.${review.decision}`)}
              />
              <span className="font-medium">{review.reviewer}</span>
              {review.reviewer === login ? <span className="text-xs text-fg-subtle">{t("you")}</span> : null}
              {review.role ? <span className="text-muted-foreground">{tReview("asRole", { role: tRole(review.role) })}</span> : null}
              {from !== null ? <VersionChip number={review.version} /> : null}
              {from !== null ? <span className="text-xs text-fg-subtle">{t("fromVersion", { version: from })}</span> : null}
              <span className="ml-auto text-xs text-fg-subtle">
                <When value={review.created_at} />
              </span>
            </p>
            {review.body ? <p className="whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{review.body}</p> : null}
          </li>
        );
      })}
    </ul>
  );
}

/** Approve, or ask for changes with a note, on one use case; each is one POST and the page reads the version again. */
function CaseControls({
  project,
  slug,
  version,
  useCase,
  actions,
  titleId,
}: {
  project: string;
  slug: string;
  version: number;
  useCase: SrsUseCase;
  actions: CaseActions;
  titleId: string;
}) {
  const t = useTranslations("srs.review.cases");
  const tReview = useTranslations("srs.review");
  const ids = useId();
  const refresh = useDocumentRefresh(project, slug);
  const failure = useWriteFailure();
  const toggle = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState<SrsReviewIn["decision"] | null>(null);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const name = caseName(useCase);
  const own = ownReview(useCase, actions.login);

  const send = async (decision: SrsReviewIn["decision"]) => {
    if (busy) return;
    if (decision === "request_changes" && !body.trim()) {
      setMissing(true);
      return;
    }
    setBusy(decision);
    setRefused(null);
    try {
      const decided = await reviewUseCase(browserApi(), project, slug, version, caseRef(useCase), {
        decision,
        role: actions.role || null,
        body: decision === "request_changes" ? body.trim() : "",
      });
      await refresh();
      notify({
        tone: "success",
        text:
          decision === "request_changes"
            ? t("requestedToast", { name })
            : decided.state === "approved"
              ? t("versionApprovedToast", { name, version })
              : t("approvedToast", { name }),
      });
      setOpen(false);
      setBody("");
      requestAnimationFrame(() => toggle.current?.focus());
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
      // The hub's gate held it, or the use case changed under the page: read the version and its findings again.
      if (isApiError(error) && (error.info.status === 409 || error.info.status === 422)) void refresh();
    } finally {
      setBusy(null);
    }
  };

  const submitRequest = (event: FormEvent) => {
    event.preventDefault();
    void send("request_changes");
  };

  return (
    <div className="flex flex-col gap-3">
      <div role="group" aria-labelledby={titleId} className="flex flex-wrap items-center gap-2">
        {own?.decision === "approve" ? null : (
          <Button
            type="button"
            size="sm"
            variant="secondary"
            busy={busy === "approve"}
            disabled={actions.locked || busy === "request_changes"}
            aria-label={t("approveFor", { name })}
            aria-describedby={actions.locked ? actions.lockId : undefined}
            onClick={() => void send("approve")}
            data-testid="srs-case-approve"
          >
            {actions.locked ? <Lock aria-hidden="true" /> : <CircleCheck aria-hidden="true" />}
            {busy === "approve" ? t("approving") : tReview("decisions.approve")}
          </Button>
        )}
        <Button
          ref={toggle}
          type="button"
          size="sm"
          variant="secondary"
          aria-expanded={open}
          aria-controls={`${ids}-form`}
          aria-label={t("requestFor", { name })}
          disabled={busy !== null}
          onClick={() => {
            setOpen((now) => !now);
            setMissing(false);
          }}
          data-testid="srs-case-request"
        >
          <Undo2 aria-hidden="true" />
          {tReview("decisions.request_changes")}
        </Button>
      </div>
      {open ? (
        <form id={`${ids}-form`} onSubmit={submitRequest} className="flex flex-col gap-2" data-testid="srs-case-form">
          <Label htmlFor={`${ids}-note`}>{t("noteLabel", { name })}</Label>
          <Textarea
            id={`${ids}-note`}
            value={body}
            onChange={(event) => {
              setBody(event.target.value);
              if (event.target.value.trim()) setMissing(false);
            }}
            rows={3}
            maxLength={MAX_REVIEW_CHARS}
            autoFocus
            aria-invalid={missing || undefined}
            aria-describedby={missing ? `${ids}-missing` : undefined}
            data-testid="srs-case-note"
          />
          {missing ? (
            <p id={`${ids}-missing`} className="text-xs font-medium text-danger" role="alert">
              {tReview("noteMissing")}
            </p>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy !== null}
              onClick={() => {
                setOpen(false);
                setMissing(false);
                toggle.current?.focus();
              }}
            >
              {t("cancel")}
            </Button>
            <Button type="submit" size="sm" busy={busy === "request_changes"} data-testid="srs-case-send">
              {busy === "request_changes" ? tReview("submitting") : t("send")}
            </Button>
          </div>
        </form>
      ) : null}
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
    </div>
  );
}

/**
 * One use case of the version: its code (linking to its page), title, what the version does to it and where it stands,
 * the reviews that count, and, for a writer who may review it, Approve and Request changes.
 */
export function UseCaseRow({
  project,
  slug,
  version,
  approved,
  useCase,
  actions,
}: {
  project: string;
  slug: string;
  version: number;
  approved: number | null;
  useCase: SrsUseCase;
  actions: CaseActions | null;
}) {
  const titleId = useId();
  const ref = caseRef(useCase);
  return (
    <li className="flex flex-col gap-3 py-4 first:pt-0 last:pb-0" data-testid="srs-case" data-state={useCase.state} data-key={useCase.key}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5">
        <CodeChip code={ref} href={itemHref(project, slug, ref, caseVersion(useCase, version, approved))} />
        <h3 id={titleId} className="min-w-0 text-sm font-medium [overflow-wrap:anywhere]">
          {useCase.title}
        </h3>
        <span className="flex flex-wrap items-center gap-2 sm:ml-auto">
          <ChangeBadge change={useCase.change} />
          <CaseStateBadge state={useCase.state} />
        </span>
      </div>
      <CaseReviews useCase={useCase} version={version} login={actions?.login ?? null} />
      {actions ? <CaseControls project={project} slug={slug} version={version} useCase={useCase} actions={actions} titleId={titleId} /> : null}
    </li>
  );
}
