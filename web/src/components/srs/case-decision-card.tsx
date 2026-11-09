"use client";

import { ArrowRight, CircleCheck, Lock, Undo2 } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { ListSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { reviewUseCase, type SrsReviewIn, type SrsUseCase } from "./api";
import type { ApproveGate, CaseAccessKind } from "./case-review-model";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { caseHref, reviewHref, reviewsHref } from "./links";
import type { QueueCard } from "./review-queue-model";
import { StepCard } from "./step-card";
import { caseRef, ownReview } from "./use-case-model";

const MAX_REVIEW_CHARS = 10_000; // evo_agents.hub.tables_srs.MAX_REVIEW_CHARS
type Decision = SrsReviewIn["decision"];
type Done = { decision: Decision; approvedAll: boolean };

/** Why Agree is not open yet, in one plain line. */
function GateLine({ id, gate }: { id: string; gate: NonNullable<ApproveGate> }) {
  const t = useTranslations("srs.caseReview.decide.lock");
  const text =
    gate.kind === "ba"
      ? t("ba")
      : gate.elsewhere === 0
        ? t("here", { count: gate.here })
        : gate.here === 0
          ? t("elsewhere", { count: gate.elsewhere })
          : t("both", { here: gate.here, elsewhere: gate.elsewhere });
  return (
    <p id={id} className="flex items-start gap-1.5 text-[13px] leading-[18px] text-attention" data-testid="srs-case-gate" data-gate={gate.kind}>
      <Lock className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      {text}
    </p>
  );
}

/** After a decision: what it did, the next use case waiting for the viewer, back to the list, or change it. */
function DonePanel({ project, done, author, next, onAgain }: { project: string; done: Done; author: string; next: QueueCard | null; onAgain: () => void }) {
  const t = useTranslations("srs.caseReview.decide");
  const heading = useRef<HTMLParagraphElement>(null);
  useEffect(() => heading.current?.focus(), []);
  const approve = done.decision === "approve";
  const nextHref = next
    ? next.useCase
      ? caseHref(project, next.version.document, next.version.number, next.useCase.key)
      : reviewHref(project, next.version.document, next.version.number)
    : null;
  const Icon = approve ? CircleCheck : Undo2;
  return (
    <div role="status" className="flex flex-col gap-3 rounded-md bg-surface-sunken p-4" data-testid="srs-case-done" data-decision={done.decision}>
      <p ref={heading} tabIndex={-1} className="flex items-start gap-2 text-sm font-medium outline-none">
        <Icon className={`mt-0.5 size-4 shrink-0 ${approve ? "text-success" : "text-attention"}`} aria-hidden="true" />
        {approve ? (done.approvedAll ? t("done.approvedAll") : t("done.approve")) : t("done.request_changes", { author })}
      </p>
      <div className="flex flex-wrap gap-2">
        {nextHref ? (
          <Button asChild>
            <Link href={nextHref} data-testid="srs-case-next">
              {t("next")}
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        ) : null}
        <Button asChild variant={nextHref ? "secondary" : "default"}>
          <Link href={reviewsHref(project)} data-testid="srs-case-list">
            {t("list")}
          </Link>
        </Button>
        <Button type="button" variant="ghost" onClick={onAgain}>
          {t("again")}
        </Button>
      </div>
    </div>
  );
}

/**
 * Step 4, the decision: Agree, or Ask for changes with a note saying what to change, as one review of the use case
 * (the hub keeps the viewer's last). While something holds an approval, Agree is disabled with one plain line saying
 * what opens it; someone who may not review is told why. After a decision the card says what it did and offers the
 * next use case waiting for the viewer, or the list.
 */
export function CaseDecisionCard({
  project,
  slug,
  number,
  useCase,
  author,
  access,
  gate,
  login,
  next,
}: {
  project: string;
  slug: string;
  number: number;
  useCase: SrsUseCase | null;
  author: string;
  access: CaseAccessKind;
  gate: ApproveGate;
  login: string | null;
  next: QueueCard | null;
}) {
  const t = useTranslations("srs.caseReview.decide");
  const format = useFormatter();
  const ids = useId();
  const refresh = useDocumentRefresh(project, slug);
  const failure = useWriteFailure();
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState<Decision | null>(null);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const [done, setDone] = useState<Done | null>(null);
  const own = useCase ? ownReview(useCase, login) : null;

  const send = async (decision: Decision) => {
    if (busy || !useCase) return;
    if (decision === "request_changes" && !body.trim()) {
      setMissing(true);
      return;
    }
    setBusy(decision);
    setRefused(null);
    try {
      const decided = await reviewUseCase(browserApi(), project, slug, number, caseRef(useCase), {
        decision,
        role: null,
        body: decision === "request_changes" ? body.trim() : "",
      });
      await refresh();
      setDone({ decision, approvedAll: decided.state === "approved" });
      setOpen(false);
      setBody("");
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
      if (isApiError(error) && (error.info.status === 409 || error.info.status === 422)) void refresh();
    } finally {
      setBusy(null);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send("request_changes");
  };

  const blocked = access === "waiting" ? null : access === "review" ? null : t(`blocked.${access}`);
  return (
    <StepCard id="srs-step-decide" title={t("title")} testId="srs-case-decide">
      {access === "waiting" ? <ListSkeleton rows={1} /> : null}
      {blocked ? (
        <p className="text-[13px] text-muted-foreground" data-testid="srs-case-blocked" data-access={access}>
          {blocked}
        </p>
      ) : null}
      {access === "review" && done ? <DonePanel project={project} done={done} author={author} next={next} onAgain={() => setDone(null)} /> : null}
      {access === "review" && !done && useCase ? (
        <>
          {own ? (
            <p className="text-[13px] text-muted-foreground" data-testid="srs-case-own" data-decision={own.decision}>
              {t(`own.${own.decision}`, { date: format.dateTime(new Date(own.created_at), { dateStyle: "medium" }) })}
            </p>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              busy={busy === "approve"}
              disabled={gate !== null || busy === "request_changes"}
              aria-describedby={gate ? `${ids}-gate` : undefined}
              onClick={() => void send("approve")}
              data-testid="srs-case-approve"
            >
              {gate ? <Lock aria-hidden="true" /> : <CircleCheck aria-hidden="true" />}
              {busy === "approve" ? t("approving") : t("approve")}
            </Button>
            <Button
              type="button"
              variant="secondary"
              aria-expanded={open}
              aria-controls={`${ids}-form`}
              disabled={busy !== null}
              onClick={() => {
                setOpen((now) => !now);
                setMissing(false);
              }}
              data-testid="srs-case-request"
            >
              <Undo2 aria-hidden="true" />
              {t("request")}
            </Button>
          </div>
          {gate ? <GateLine id={`${ids}-gate`} gate={gate} /> : null}
          {open ? (
            <form id={`${ids}-form`} onSubmit={submit} className="flex flex-col gap-2" data-testid="srs-case-form">
              <Label htmlFor={`${ids}-note`}>{t("noteLabel")}</Label>
              <Textarea
                id={`${ids}-note`}
                value={body}
                onChange={(event) => {
                  setBody(event.target.value);
                  if (event.target.value.trim()) setMissing(false);
                }}
                rows={4}
                maxLength={MAX_REVIEW_CHARS}
                aria-required="true"
                autoFocus
                aria-invalid={missing || undefined}
                aria-describedby={missing ? `${ids}-missing` : undefined}
                data-testid="srs-case-note"
              />
              {missing ? (
                <p id={`${ids}-missing`} className="text-xs font-medium text-danger" role="alert">
                  {t("noteMissing")}
                </p>
              ) : null}
              <div className="flex flex-wrap justify-end gap-2">
                <Button type="button" variant="ghost" disabled={busy !== null} onClick={() => setOpen(false)}>
                  {t("cancel")}
                </Button>
                <Button type="submit" variant="secondary" busy={busy === "request_changes"} data-testid="srs-case-send">
                  {busy === "request_changes" ? t("sending") : t("send")}
                </Button>
              </div>
            </form>
          ) : null}
          {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
        </>
      ) : null}
    </StepCard>
  );
}
