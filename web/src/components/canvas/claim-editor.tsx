"use client";

import { useQuery } from "@tanstack/react-query";
import { GitMerge, Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { InlineError } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { signalKeysQuery } from "@/lib/signal-queries";

import { type CanvasClaimDetail, type CanvasContent, editClaims } from "./api";
import { ClaimFields } from "./claim-fields";
import { SectionCard } from "./chrome";
import { FieldChanges } from "./field-diff";
import { useCanvasWrite } from "./hooks";
import { changeOf, type ClaimForm, conflictsOf, formOf, LIMITS, rebase, testCardOf, testingLacks } from "./model";

/** The content of a claim as it is now: what an edit starts from. */
function contentOf(claim: CanvasClaimDetail): CanvasContent {
  const { cell, position, text, status, confidence, test_card, archived, use_cases, signal_keys } = claim;
  return { cell, position, text, status, confidence, test_card, archived, use_cases, signal_keys };
}

/**
 * A claim's editor: the form starts from the rev the page read and the edit names that rev. When someone changed the
 * claim since, the hub refuses the whole edit (409) and the editor keeps the writer's fields, says who wrote which rev
 * and what they changed, and lets the writer carry their own changes over to the newest rev (what the other writer
 * changed stays) or start again from it.
 */
export function ClaimEditor({ project, slug, claim, onClose }: { project: string; slug: string; claim: CanvasClaimDetail; onClose: () => void }) {
  const t = useTranslations("canvas.editor");
  const ids = useId();
  const [base, setBase] = useState(() => ({ rev: claim.rev, content: contentOf(claim) }));
  const [form, setForm] = useState<ClaimForm>(() => formOf(contentOf(claim)));
  const [reason, setReason] = useState("");
  const [tried, setTried] = useState(false);
  const keys = useQuery(signalKeysQuery(browserApi, project));
  const { busy, refused, run, clear } = useCanvasWrite(project, slug);
  const conflict = refused ? conflictsOf(refused.error)[0] : undefined;
  const textMissing = tried && !form.text.trim();
  const lacks = form.status === "testing" ? testingLacks(testCardOf(form)) : [];
  const newer = claim.rev !== base.rev;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!form.text.trim() || lacks.length > 0 || busy) return;
    const change = changeOf(form, { content: base.content, code: claim.code, rev: base.rev });
    if (!change) {
      notify({ tone: "info", text: t("nothing") });
      onClose();
      return;
    }
    void run(async () => {
      await editClaims(browserApi(), project, slug, [change], reason.trim());
      return t("saved", { claim: claim.code });
    }, onClose);
  };
  const keepMine = () => {
    const newest = contentOf(claim);
    setForm((held) => rebase(held, base.content, newest));
    setBase({ rev: claim.rev, content: newest });
    clear();
  };
  const startAgain = () => {
    setBase({ rev: claim.rev, content: contentOf(claim) });
    setForm(formOf(contentOf(claim)));
    clear();
  };
  return (
    <SectionCard title={t("heading", { claim: claim.code, rev: base.rev })} headingId="claim-editor-title" testId="claim-editor">
      <form onSubmit={submit} noValidate className="flex flex-col gap-5">
        {conflict ? (
          <Banner
            icon={GitMerge}
            tone="attention"
            role="alert"
            title={t("conflict", { claim: conflict.claim, rev: conflict.rev, login: conflict.changedBy, base: conflict.baseRev })}
            testId="claim-conflict"
            actions={
              newer ? (
                <>
                  <Button type="button" size="sm" variant="outline" onClick={keepMine} data-testid="claim-conflict-keep">
                    {t("keepMine", { rev: claim.rev })}
                  </Button>
                  <Button type="button" size="sm" variant="ghost" onClick={startAgain} data-testid="claim-conflict-reset">
                    {t("startAgain", { rev: claim.rev })}
                  </Button>
                </>
              ) : null
            }
          >
            <div className="flex flex-col gap-2">
              <p>{t("conflictText")}</p>
              <FieldChanges changes={conflict.changes} testId="claim-conflict-changes" />
            </div>
          </Banner>
        ) : refused ? (
          <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} />
        ) : null}
        <ClaimFields form={form} onChange={setForm} keys={keys.data ?? []} textMissing={textMissing} lacks={tried ? lacks : []} />
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-reason`}>{t("reason")}</Label>
          <Input id={`${ids}-reason`} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={LIMITS.reason} placeholder={t("reasonPlaceholder")} data-testid="claim-reason" />
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("cancel")}
          </Button>
          <Button type="submit" busy={busy} disabled={Boolean(conflict) && !newer} data-testid="claim-save">
            <Save aria-hidden="true" />
            {t("save")}
          </Button>
        </div>
      </form>
    </SectionCard>
  );
}
