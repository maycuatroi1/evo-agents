"use client";

import { ClipboardCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { type ReviewRole, reviewVersion, type SrsReviewIn, type SrsVersion } from "./api";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";

const MAX_REVIEW_CHARS = 10_000; // evo_agents.hub.tables_srs.MAX_REVIEW_CHARS
type Decision = SrsReviewIn["decision"];
const DECISIONS: Decision[] = ["approve", "request_changes"];

/**
 * A writer's review of a version in review: approve it, or ask for changes, which sends it back to draft and needs a
 * note saying what to change. The role it counts for is the hub's pick unless the reviewer holds several and picks one.
 */
export function ReviewForm({ project, slug, version, roles }: { project: string; slug: string; version: SrsVersion; roles: ReviewRole[] }) {
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

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (decision === "request_changes" && !body.trim()) {
      setMissingNote(true);
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      const decided = await reviewVersion(browserApi(), project, slug, version.number, { decision, role: role || null, body: body.trim() });
      await refresh();
      const text =
        decided.state === "approved"
          ? t("approvedToast", { version: version.number })
          : decision === "approve"
            ? t("approvalToast", { version: version.number })
            : t("returnedToast", { version: version.number });
      notify({ tone: "success", text });
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
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
          value={decision}
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
          {DECISIONS.map((value) => (
            <Label
              key={value}
              htmlFor={`${ids}-${value}`}
              className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 font-normal transition-colors hover:bg-muted/50 has-data-checked:border-brand/40 has-data-checked:bg-surface-selected"
            >
              <RadioGroupItem id={`${ids}-${value}`} value={value} className="mt-0.5" aria-labelledby={`${ids}-${value}-name`} aria-describedby={`${ids}-${value}-hint`} />
              <span className="flex flex-col gap-1">
                <span id={`${ids}-${value}-name`} className="text-sm font-medium">
                  {t(`decisions.${value}`)}
                </span>
                <span id={`${ids}-${value}-hint`} className="text-xs leading-snug text-muted-foreground">
                  {t(`decisionHints.${value}`)}
                </span>
              </span>
            </Label>
          ))}
        </RadioGroup>
      </fieldset>
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
        <Label htmlFor={`${ids}-note`}>{decision === "request_changes" ? t("noteRequired") : t("note")}</Label>
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
