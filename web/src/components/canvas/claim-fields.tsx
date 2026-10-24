"use client";

import { useTranslations } from "next-intl";
import { useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

import { CELLS, type ClaimForm, CONFIDENCES, DELIVERIES, LIMITS, type SignalKeyOption, STATUSES } from "./model";
import { TestCardFields } from "./test-card-fields";

const FIELD = "flex min-w-0 flex-col gap-1.5";

/** The signal keys a claim links, as a list of checkboxes; a link the registry no longer lists stays, checked. */
function KeyChecks({ form, onChange, keys }: { form: ClaimForm; onChange: (form: ClaimForm) => void; keys: readonly SignalKeyOption[] }) {
  const t = useTranslations("canvas.form");
  const names = [...new Set([...keys.map((key) => key.name), ...form.signalKeys])].sort();
  if (names.length === 0) return <p className="text-xs text-fg-subtle">{t("noKeys")}</p>;
  const toggle = (name: string, on: boolean) =>
    onChange({ ...form, signalKeys: on ? [...form.signalKeys, name].sort() : form.signalKeys.filter((held) => held !== name) });
  return (
    <ul className="max-h-48 overflow-y-auto rounded-sm border" data-testid="claim-keys">
      {names.map((name) => {
        const checked = form.signalKeys.includes(name);
        const key = keys.find((item) => item.name === name);
        return (
          <li key={name} className="border-t first:border-t-0">
            <label className={cn("flex min-h-9 cursor-pointer items-center gap-2.5 px-3 py-1.5 transition-colors hover:bg-accent max-md:min-h-11", checked && "bg-surface-selected")}>
              <input
                type="checkbox"
                checked={checked}
                onChange={(event) => toggle(name, event.target.checked)}
                className="size-4 shrink-0 cursor-pointer accent-primary"
                data-key={name}
              />
              <span className="min-w-0 font-mono text-xs [overflow-wrap:anywhere] text-foreground">{name}</span>
              {key ? <span className="ml-auto text-xs text-fg-subtle">{key.unit || t(`keyKind.${key.kind}`)}</span> : null}
            </label>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The fields of a claim, for a new one and for an edit: its text, cell, status and confidence, its test card, and the
 * use cases and signal keys it links. Going to testing needs a hypothesis, a test and a deadline (`lacks` names what
 * is missing once the writer tried to save); validated is the hub's to refuse without real evidence.
 */
export function ClaimFields({
  form,
  onChange,
  keys,
  textMissing,
  lacks,
}: {
  form: ClaimForm;
  onChange: (form: ClaimForm) => void;
  keys: readonly SignalKeyOption[];
  textMissing: boolean;
  lacks: readonly string[];
}) {
  const t = useTranslations("canvas.form");
  const tCell = useTranslations("canvas.cell");
  const tStatus = useTranslations("canvas.status");
  const tConfidence = useTranslations("canvas.confidence");
  const tDelivery = useTranslations("canvas.delivery");
  const ids = useId();
  const set = <K extends keyof ClaimForm>(field: K, value: ClaimForm[K]) => onChange({ ...form, [field]: value });
  return (
    <div className="flex flex-col gap-5" data-testid="claim-fields">
      <div className={FIELD}>
        <Label htmlFor={`${ids}-text`}>{t("text")}</Label>
        <Textarea
          id={`${ids}-text`}
          value={form.text}
          onChange={(event) => set("text", event.target.value)}
          rows={3}
          maxLength={LIMITS.text}
          placeholder={t("textPlaceholder")}
          aria-required="true"
          aria-invalid={textMissing || undefined}
          aria-describedby={textMissing ? `${ids}-text-error` : undefined}
          data-testid="claim-text"
        />
        {textMissing ? (
          <p id={`${ids}-text-error`} className="text-xs font-medium text-danger">
            {t("textMissing")}
          </p>
        ) : null}
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <div className={FIELD}>
          <Label htmlFor={`${ids}-cell`}>{t("cell")}</Label>
          <NativeSelect id={`${ids}-cell`} value={form.cell} onChange={(event) => set("cell", event.target.value as ClaimForm["cell"])} className="w-full" data-testid="claim-cell">
            {CELLS.map((cell) => (
              <NativeSelectOption key={cell} value={cell}>
                {tCell(cell)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-status`}>{t("status")}</Label>
          <NativeSelect
            id={`${ids}-status`}
            value={form.status}
            onChange={(event) => set("status", event.target.value as ClaimForm["status"])}
            className="w-full"
            aria-describedby={`${ids}-status-help`}
            data-testid="claim-status-select"
          >
            {STATUSES.map((status) => (
              <NativeSelectOption key={status} value={status}>
                {tStatus(status)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-confidence`}>{t("confidence")}</Label>
          <NativeSelect
            id={`${ids}-confidence`}
            value={form.confidence}
            onChange={(event) => set("confidence", event.target.value as ClaimForm["confidence"])}
            className="w-full"
            data-testid="claim-confidence"
          >
            {CONFIDENCES.map((confidence) => (
              <NativeSelectOption key={confidence} value={confidence}>
                {tConfidence(confidence)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      </div>
      <p id={`${ids}-status-help`} className="-mt-3 text-xs text-fg-subtle">
        {form.status === "validated" ? t("validatedHelp") : form.status === "testing" ? t("testingHelp") : t("statusHelp")}
      </p>
      <div className={FIELD}>
        <Label htmlFor={`${ids}-delivery`}>{t("delivery")}</Label>
        <NativeSelect
          id={`${ids}-delivery`}
          value={form.delivery}
          onChange={(event) => set("delivery", event.target.value as ClaimForm["delivery"])}
          className="w-full sm:w-1/3"
          aria-describedby={`${ids}-delivery-help`}
          data-testid="claim-delivery"
        >
          {DELIVERIES.map((delivery) => (
            <NativeSelectOption key={delivery} value={delivery}>
              {tDelivery(delivery)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <p id={`${ids}-delivery-help`} className="text-xs text-fg-subtle">
          {t("deliveryHelp")}
        </p>
      </div>
      <TestCardFields form={form} onChange={onChange} keys={keys} lacks={lacks} />
      <fieldset className="flex min-w-0 flex-col gap-4">
        <legend className="mb-3 text-[13px] font-semibold text-foreground">{t("links")}</legend>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-use-cases`}>{t("useCases")}</Label>
          <Input
            id={`${ids}-use-cases`}
            value={form.useCases}
            onChange={(event) => set("useCases", event.target.value)}
            placeholder={t("useCasesPlaceholder")}
            aria-describedby={`${ids}-use-cases-help`}
            data-testid="claim-use-cases"
          />
          <p id={`${ids}-use-cases-help`} className="text-xs text-fg-subtle">
            {t("useCasesHelp", { max: LIMITS.links })}
          </p>
        </div>
        <div className={FIELD}>
          <span className="text-sm font-medium" id={`${ids}-keys`}>
            {t("signalKeys")}
          </span>
          <div role="group" aria-labelledby={`${ids}-keys`}>
            <KeyChecks form={form} onChange={onChange} keys={keys} />
          </div>
        </div>
      </fieldset>
    </div>
  );
}
