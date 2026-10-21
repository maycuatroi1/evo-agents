"use client";

import { ClipboardCheck, Hourglass, Lock } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { type ReviewRole, reviewVersion, type SrsReviewIn, type SrsVersion } from "./api";
import { ChatLink } from "./finding-item";
import { ReviewRunRef } from "./findings-card";
import type { ApproveLock } from "./findings-model";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";

const MAX_REVIEW_CHARS = 10_000; // evo_agents.hub.tables_srs.MAX_REVIEW_CHARS
type Decision = SrsReviewIn["decision"];
const DECISIONS: Decision[] = ["approve", "request_changes"];

/**
 * Why Approve is locked, under the choices: the open findings to decide, with the chat they are settled in, in
 * `attention` since a person settles them; or @ba's review of the round, which names its run and its state.
 */
function LockNote({ id, project, round, lock }: { id: string; project: string; round: number; lock: ApproveLock }) {
  const t = useTranslations("srs.review.lock");
  return (
    <div id={id}>
      {lock.kind === "findings" ? (
        <Banner
          icon={Lock}
          tone="attention"
          title={t("findingsTitle", { count: lock.count })}
          actions={<ChatLink link={lock.chat} label={t("openChat")} testId="srs-review-lock-chat" />}
          testId="srs-review-lock"
        >
          {t("findingsText")}
        </Banner>
      ) : (
        <Banner icon={Hourglass} title={t("reviewTitle", { round })} actions={<ReviewRunRef project={project} run={lock.run} />} testId="srs-review-lock">
          {t("reviewText")}
        </Banner>
      )}
    </div>
  );
}

/**
 * A writer's review of a version in review: approve it, or ask for changes, which sends it back to draft and needs a
 * note saying what to change. The role it counts for is the hub's pick unless the reviewer holds several and picks one.
 * While `lock` says the hub would refuse an approval (open findings to decide, @ba's review still running), Approve is
 * disabled with the reason under the choices and Request changes is the one taken; the hub stays the gate, and its 409
 * shows as any refusal does while the page reads the document and its findings again.
 */
export function ReviewForm({
  project,
  slug,
  version,
  roles,
  lock = null,
}: {
  project: string;
  slug: string;
  version: SrsVersion;
  roles: ReviewRole[];
  lock?: ApproveLock | null;
}) {
  const t = useTranslations("srs.review");
  const tRole = useTranslations("srs.role");
  const ids = useId();
  const refresh = useDocumentRefresh(project, slug);
  const failure = useWriteFailure();
  const [decision, setDecision] = useState<Decision>("approve");
  const [role, setRole] = useState<ReviewRole | "">("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [missingNote, setMissingNote] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  // Locked, Approve is never the one taken; it is again once the lock goes, unless the reviewer picked the other.
  const chosen: Decision = lock && decision === "approve" ? "request_changes" : decision;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (chosen === "request_changes" && !body.trim()) {
      setMissingNote(true);
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      const decided = await reviewVersion(browserApi(), project, slug, version.number, { decision: chosen, role: role || null, body: body.trim() });
      await refresh();
      const text =
        decided.state === "approved"
          ? t("approvedToast", { version: version.number })
          : chosen === "approve"
            ? t("approvalToast", { version: version.number })
            : t("returnedToast", { version: version.number });
      notify({ tone: "success", text });
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
      // The hub's gate held it: read the findings and the review run again, so the lock and its reason show.
      if (isApiError(error) && error.info.status === 409) void refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4" aria-labelledby={`${ids}-title`} data-testid="srs-review-form">
      <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
        {t("formTitle", { version: version.number })}
      </h2>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1.5 text-sm font-medium">{t("decision")}</legend>
        <RadioGroup
          value={chosen}
          onValueChange={(value) => {
            if (value === "approve" || value === "request_changes") {
              setDecision(value);
              setMissingNote(false);
            }
          }}
          className="gap-2 sm:grid-cols-2"
          name="decision"
          data-testid="srs-review-decision"
        >
          {DECISIONS.map((value) => {
            const locked = lock !== null && value === "approve";
            return (
              <Label
                key={value}
                htmlFor={`${ids}-${value}`}
                data-locked={locked || undefined}
                className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 font-normal transition-colors hover:bg-muted/50 has-data-checked:border-brand/40 has-data-checked:bg-surface-selected data-locked:cursor-not-allowed data-locked:bg-muted/40 data-locked:hover:bg-muted/40"
              >
                <RadioGroupItem
                  id={`${ids}-${value}`}
                  value={value}
                  disabled={locked}
                  className="mt-0.5"
                  aria-labelledby={`${ids}-${value}-name`}
                  aria-describedby={locked ? `${ids}-${value}-hint ${ids}-lock` : `${ids}-${value}-hint`}
                  data-testid={`srs-review-decision-${value}`}
                />
                <span className="flex flex-col gap-1">
                  <span id={`${ids}-${value}-name`} className="inline-flex items-center gap-1.5 text-sm font-medium">
                    {locked ? <Lock className="size-3.5 text-muted-foreground" aria-hidden="true" /> : null}
                    {t(`decisions.${value}`)}
                  </span>
                  <span id={`${ids}-${value}-hint`} className="text-xs leading-snug text-muted-foreground">
                    {t(`decisionHints.${value}`)}
                  </span>
                </span>
              </Label>
            );
          })}
        </RadioGroup>
      </fieldset>
      {lock ? <LockNote id={`${ids}-lock`} project={project} round={version.round} lock={lock} /> : null}
      {roles.length > 1 ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-role`}>{t("role")}</Label>
          <NativeSelect id={`${ids}-role`} value={role} onChange={(event) => setRole(event.target.value as ReviewRole | "")} data-testid="srs-review-role">
            <option value="">{t("roleAuto")}</option>
            {roles.map((held) => (
              <option key={held} value={held}>
                {tRole(held)}
              </option>
            ))}
          </NativeSelect>
        </div>
      ) : null}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-note`}>{chosen === "request_changes" ? t("noteRequired") : t("note")}</Label>
        <Textarea
          id={`${ids}-note`}
          value={body}
          onChange={(event) => {
            setBody(event.target.value);
            if (event.target.value.trim()) setMissingNote(false);
          }}
          rows={4}
          maxLength={MAX_REVIEW_CHARS}
          aria-invalid={missingNote || undefined}
          aria-describedby={missingNote ? `${ids}-note-error` : undefined}
          data-testid="srs-review-note"
        />
        {missingNote ? (
          <p id={`${ids}-note-error`} className="text-xs font-medium text-danger" role="alert">
            {t("noteMissing")}
          </p>
        ) : null}
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <Button type="submit" busy={busy} className="self-end" data-testid="srs-review-submit">
        <ClipboardCheck aria-hidden="true" />
        {busy ? t("submitting") : t("submit")}
      </Button>
    </form>
  );
}
