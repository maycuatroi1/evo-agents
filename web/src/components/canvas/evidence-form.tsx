"use client";

import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { type AddedEvidenceKind, addEvidence, type Direction } from "./api";
import { useCanvasWrite } from "./hooks";
import { ADDED_KINDS, DIRECTIONS, LIMITS } from "./model";

const FIELD = "flex min-w-0 flex-col gap-1.5";
const EMPTY = { kind: "interview" as AddedEvidenceKind, direction: "supports" as Direction, summary: "", url: "", observed: "", synthetic: false };

/**
 * A writer adds evidence to a claim: its kind, which way it points, what it says, a link and the day it was taken, and
 * whether it rests on simulated people (a simulation always does). A doc or a simulation never validates a claim; the
 * hub writes evidence of kind signal and feedback itself, from the value check.
 */
export function EvidenceForm({ project, slug, claim, onDone }: { project: string; slug: string; claim: string; onDone: () => void }) {
  const t = useTranslations("canvas.evidenceForm");
  const tKind = useTranslations("canvas.evidenceKind");
  const tDirection = useTranslations("canvas.direction");
  const ids = useId();
  const { busy, refused, run } = useCanvasWrite(project, slug);
  const [form, setForm] = useState(EMPTY);
  const [tried, setTried] = useState(false);
  const simulation = form.kind === "simulation";
  const missing = tried && !form.summary.trim();
  const badUrl = form.url.trim() !== "" && !/^https?:\/\//i.test(form.url.trim());
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!form.summary.trim() || badUrl || busy) return;
    void run(async () => {
      await addEvidence(browserApi(), project, slug, claim, {
        kind: form.kind,
        direction: form.direction,
        summary: form.summary.trim(),
        url: form.url.trim() || null,
        observed_on: form.observed || null,
        synthetic: simulation || form.synthetic,
      });
      return t("added", { claim });
    }, () => {
      setForm(EMPTY);
      setTried(false);
      onDone();
    });
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4 rounded-md border bg-surface-sunken/50 p-3" data-testid="evidence-form">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className={FIELD}>
          <Label htmlFor={`${ids}-kind`}>{t("kind")}</Label>
          <NativeSelect id={`${ids}-kind`} value={form.kind} onChange={(event) => setForm((held) => ({ ...held, kind: event.target.value as AddedEvidenceKind }))} className="w-full" data-testid="evidence-form-kind">
            {ADDED_KINDS.map((kind) => (
              <NativeSelectOption key={kind} value={kind}>
                {tKind(kind)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-direction`}>{t("direction")}</Label>
          <NativeSelect
            id={`${ids}-direction`}
            value={form.direction}
            onChange={(event) => setForm((held) => ({ ...held, direction: event.target.value as Direction }))}
            className="w-full"
            data-testid="evidence-form-direction"
          >
            {DIRECTIONS.map((direction) => (
              <NativeSelectOption key={direction} value={direction}>
                {tDirection(direction)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      </div>
      <div className={FIELD}>
        <Label htmlFor={`${ids}-summary`}>{t("summary")}</Label>
        <Textarea
          id={`${ids}-summary`}
          value={form.summary}
          onChange={(event) => setForm((held) => ({ ...held, summary: event.target.value }))}
          rows={3}
          maxLength={LIMITS.summary}
          placeholder={t("summaryPlaceholder")}
          aria-required="true"
          aria-invalid={missing || undefined}
          aria-describedby={missing ? `${ids}-summary-error` : undefined}
          data-testid="evidence-form-summary"
        />
        {missing ? (
          <p id={`${ids}-summary-error`} className="text-xs font-medium text-danger">
            {t("summaryMissing")}
          </p>
        ) : null}
      </div>
      <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_12rem]">
        <div className={FIELD}>
          <Label htmlFor={`${ids}-url`}>{t("url")}</Label>
          <Input
            id={`${ids}-url`}
            type="url"
            inputMode="url"
            value={form.url}
            onChange={(event) => setForm((held) => ({ ...held, url: event.target.value }))}
            maxLength={LIMITS.url}
            placeholder="https://"
            aria-invalid={badUrl || undefined}
            aria-describedby={badUrl ? `${ids}-url-error` : undefined}
          />
          {badUrl ? (
            <p id={`${ids}-url-error`} className="text-xs font-medium text-danger">
              {t("urlBad")}
            </p>
          ) : null}
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-observed`}>{t("observed")}</Label>
          <Input id={`${ids}-observed`} type="date" value={form.observed} onChange={(event) => setForm((held) => ({ ...held, observed: event.target.value }))} />
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Switch
          id={`${ids}-synthetic`}
          checked={simulation || form.synthetic}
          disabled={simulation}
          onCheckedChange={(synthetic) => setForm((held) => ({ ...held, synthetic }))}
          aria-describedby={`${ids}-synthetic-help`}
        />
        <Label htmlFor={`${ids}-synthetic`} className="font-normal">
          {t("synthetic")}
        </Label>
      </div>
      <p id={`${ids}-synthetic-help`} className="-mt-2 text-xs text-fg-subtle">
        {simulation ? t("simulationHelp") : t("syntheticHelp")}
      </p>
      {refused ? <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} /> : null}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onDone}>
          {t("cancel")}
        </Button>
        <Button type="submit" variant="secondary" busy={busy} data-testid="evidence-form-add">
          <Plus aria-hidden="true" />
          {t("add")}
        </Button>
      </div>
    </form>
  );
}
