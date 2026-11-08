"use client";

import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { settleFinding, type SrsFinding } from "./api";
import { CodeChip } from "./badges";
import { optionKey } from "./case-review-model";
import { Sources } from "./finding-item";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";

const MAX_NOTE_CHARS = 10_000; // evo_agents.hub.tables_srs.MAX_RESOLUTION_CHARS, the note of SrsSettleIn

/** What a finding is about when not the use case itself: the code of the business rule under it. */
function About({ finding, caseKey }: { finding: SrsFinding; caseKey: string }) {
  const t = useTranslations("srs.caseReview.ba");
  if (finding.item === null || finding.item === caseKey || !finding.item_code) return null;
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      {t("about")}
      <CodeChip code={finding.item_code} />
    </p>
  );
}

/** The sources of a finding, folded: node ids and file paths are there for whoever wants to check. */
export function FoldedSources({ project, slug, finding }: { project: string; slug: string; finding: SrsFinding }) {
  const t = useTranslations("srs.caseReview.ba");
  if (finding.sources.length === 0) return null;
  return (
    <details className="group text-[13px]" data-testid="srs-case-sources">
      <summary className="inline-flex min-h-8 cursor-pointer list-none items-center gap-1 text-xs text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden="true" />
        {t("sources")}
      </summary>
      <div className="mt-1 pl-4">
        <Sources project={project} slug={slug} sources={finding.sources} />
      </div>
    </details>
  );
}

/**
 * A point @ba asks the reviewer to settle, as a question: its summary, then one button per choice ("A. ...", "B. ...").
 * Picking one asks for an optional note and settles the finding with that choice; a finding with no choice takes a
 * note saying how it is settled. A reader sees the choices without buttons. The hub refuses a finding no longer open
 * (409), after which the page reads the findings again.
 */
export function FindingChoice({ project, slug, caseKey, finding, writer }: { project: string; slug: string; caseKey: string; finding: SrsFinding; writer: boolean }) {
  const t = useTranslations("srs.caseReview.ba");
  const ids = useId();
  const refresh = useDocumentRefresh(project, slug);
  const failure = useWriteFailure();
  const [picked, setPicked] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const options = finding.options;
  const choosing = options.length > 0;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!choosing && !note.trim()) {
      setMissing(true);
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      await settleFinding(browserApi(), project, slug, finding.id, choosing ? { option: picked, note: note.trim() } : { note: note.trim() });
      await refresh();
      notify({ tone: "success", text: choosing && picked !== null ? t("settledToast", { option: optionKey(picked) }) : t("settledNoteToast") });
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
      if (isApiError(error) && error.info.status === 409) void refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex min-w-0 flex-col gap-3 py-4 first:pt-0 last:pb-0" data-testid="srs-case-finding" data-finding={finding.id} data-level={finding.level}>
      <div className="flex min-w-0 flex-col gap-1">
        <p id={`${ids}-q`} className="text-sm font-medium text-foreground [overflow-wrap:anywhere]" data-testid="srs-case-question">
          {finding.summary}
        </p>
        <About finding={finding} caseKey={caseKey} />
      </div>
      {choosing ? (
        writer ? (
          <div role="group" aria-labelledby={`${ids}-q`} className="flex flex-col gap-2">
            {options.map((option, index) => (
              <Button
                key={option}
                type="button"
                variant="secondary"
                aria-pressed={picked === index}
                disabled={busy}
                onClick={() => setPicked(index)}
                className={cn("h-auto min-h-9 justify-start py-2 text-left whitespace-normal", picked === index && "border-brand/50 bg-surface-selected")}
                data-testid="srs-case-option"
              >
                <span className="font-semibold">{optionKey(index)}.</span>
                <span className="min-w-0 font-normal [overflow-wrap:anywhere]">{option}</span>
              </Button>
            ))}
          </div>
        ) : (
          <ol className="flex flex-col gap-1 text-[13px]" aria-label={t("choices")}>
            {options.map((option, index) => (
              <li key={option}>
                <span className="font-semibold">{optionKey(index)}.</span> {option}
              </li>
            ))}
          </ol>
        )
      ) : null}
      {writer && (picked !== null || !choosing) ? (
        <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-2" data-testid="srs-case-settle">
          <Label htmlFor={`${ids}-note`}>
            {choosing && picked !== null ? t("pickedNote", { option: optionKey(picked) }) : t("howSettled")}
          </Label>
          <Textarea
            id={`${ids}-note`}
            value={note}
            onChange={(event) => {
              setNote(event.target.value);
              if (event.target.value.trim()) setMissing(false);
            }}
            rows={2}
            maxLength={MAX_NOTE_CHARS}
            aria-invalid={missing || undefined}
            aria-describedby={missing ? `${ids}-missing` : undefined}
            data-testid="srs-case-settle-note"
          />
          {missing ? (
            <p id={`${ids}-missing`} className="text-xs font-medium text-danger" role="alert">
              {t("noteMissing")}
            </p>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            {choosing ? (
              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setPicked(null)}>
                {t("cancel")}
              </Button>
            ) : null}
            <Button type="submit" size="sm" variant="secondary" busy={busy} data-testid="srs-case-settle-send">
              {busy ? t("settling") : choosing ? t("settle") : t("settled")}
            </Button>
          </div>
        </form>
      ) : null}
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <FoldedSources project={project} slug={slug} finding={finding} />
    </li>
  );
}
