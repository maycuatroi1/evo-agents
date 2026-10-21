"use client";

import { useTranslations } from "next-intl";
import { useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";

import { AGGREGATES, type ClaimForm, COMPARATORS, FEEDBACK, LIMITS, type SignalKeyOption } from "./model";

const FIELD = "flex min-w-0 flex-col gap-1.5";

/** A field's error under it, for what testing needs. */
function Lacking({ id, text }: { id: string; text: string }) {
  return (
    <p id={id} className="text-xs font-medium text-danger">
      {text}
    </p>
  );
}

/**
 * The test card of a claim (Strategyzer's Test Card): what we believe, how we test it, the signal key we measure and
 * the threshold that makes it right, and how the value check compares a signal with it (an aggregate over a window of
 * days, at least so many samples, of one tenant), with the deadline by which we expect to know. A metric of feedback
 * counts the feedback @strategist judges to support the claim, so it takes no aggregate.
 */
export function TestCardFields({
  form,
  onChange,
  keys,
  lacks,
}: {
  form: ClaimForm;
  onChange: (form: ClaimForm) => void;
  keys: readonly SignalKeyOption[];
  lacks: readonly string[];
}) {
  const t = useTranslations("canvas.card");
  const ids = useId();
  const set = <K extends keyof ClaimForm>(field: K, value: ClaimForm[K]) => onChange({ ...form, [field]: value });
  const lacking = (field: "hypothesis" | "test" | "deadline") => lacks.includes(field);
  const feedback = form.metric === FEEDBACK;
  const metrics = [...new Set([...keys.filter((key) => key.kind === "number").map((key) => key.name), ...(form.metric && !feedback ? [form.metric] : [])])].sort();
  return (
    <fieldset className="flex min-w-0 flex-col gap-4" data-testid="claim-card-fields">
      <legend className="mb-3 text-[13px] font-semibold text-foreground">{t("heading")}</legend>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className={FIELD}>
          <Label htmlFor={`${ids}-hypothesis`}>{t("hypothesis")}</Label>
          <Textarea
            id={`${ids}-hypothesis`}
            value={form.hypothesis}
            onChange={(event) => set("hypothesis", event.target.value)}
            rows={2}
            maxLength={LIMITS.cardText}
            placeholder={t("hypothesisPlaceholder")}
            aria-invalid={lacking("hypothesis") || undefined}
            aria-describedby={lacking("hypothesis") ? `${ids}-hypothesis-error` : undefined}
            data-testid="card-hypothesis"
          />
          {lacking("hypothesis") ? <Lacking id={`${ids}-hypothesis-error`} text={t("needed")} /> : null}
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-test`}>{t("test")}</Label>
          <Textarea
            id={`${ids}-test`}
            value={form.test}
            onChange={(event) => set("test", event.target.value)}
            rows={2}
            maxLength={LIMITS.cardText}
            placeholder={t("testPlaceholder")}
            aria-invalid={lacking("test") || undefined}
            aria-describedby={lacking("test") ? `${ids}-test-error` : undefined}
            data-testid="card-test"
          />
          {lacking("test") ? <Lacking id={`${ids}-test-error`} text={t("needed")} /> : null}
        </div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className={FIELD}>
          <Label htmlFor={`${ids}-metric`}>{t("metric")}</Label>
          <NativeSelect
            id={`${ids}-metric`}
            value={form.metric}
            onChange={(event) => onChange({ ...form, metric: event.target.value, ...(event.target.value === FEEDBACK ? { aggregate: "" } : {}) })}
            className="w-full"
            data-testid="card-metric"
          >
            <NativeSelectOption value="">{t("noMetric")}</NativeSelectOption>
            <NativeSelectOption value={FEEDBACK}>{t("feedback")}</NativeSelectOption>
            {metrics.map((name) => (
              <NativeSelectOption key={name} value={name}>
                {name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-aggregate`}>{t("aggregate")}</Label>
          <NativeSelect
            id={`${ids}-aggregate`}
            value={feedback ? "" : form.aggregate}
            onChange={(event) => set("aggregate", event.target.value)}
            disabled={feedback}
            className="w-full"
            data-testid="card-aggregate"
          >
            <NativeSelectOption value="">{t("none")}</NativeSelectOption>
            {AGGREGATES.map((aggregate) => (
              <NativeSelectOption key={aggregate} value={aggregate}>
                {t(`aggregates.${aggregate}`)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-comparator`}>{t("comparator")}</Label>
          <NativeSelect id={`${ids}-comparator`} value={form.comparator} onChange={(event) => set("comparator", event.target.value)} className="w-full" data-testid="card-comparator">
            <NativeSelectOption value="">{t("none")}</NativeSelectOption>
            {COMPARATORS.map((comparator) => (
              <NativeSelectOption key={comparator} value={comparator}>
                {comparator}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-threshold`}>{t("threshold")}</Label>
          <Input id={`${ids}-threshold`} type="number" inputMode="decimal" step="any" value={form.threshold} onChange={(event) => set("threshold", event.target.value)} data-testid="card-threshold" />
        </div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className={FIELD}>
          <Label htmlFor={`${ids}-window`}>{t("windowDays")}</Label>
          <Input
            id={`${ids}-window`}
            type="number"
            inputMode="numeric"
            min={1}
            max={LIMITS.windowDays}
            value={form.windowDays}
            onChange={(event) => set("windowDays", event.target.value)}
            placeholder="90"
          />
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-samples`}>{t("minSamples")}</Label>
          <Input id={`${ids}-samples`} type="number" inputMode="numeric" min={1} value={form.minSamples} onChange={(event) => set("minSamples", event.target.value)} placeholder="1" />
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-tenant`}>{t("tenant")}</Label>
          <Input id={`${ids}-tenant`} value={form.tenant} onChange={(event) => set("tenant", event.target.value)} maxLength={LIMITS.tenant} placeholder={t("tenantPlaceholder")} />
        </div>
        <div className={FIELD}>
          <Label htmlFor={`${ids}-deadline`}>{t("deadline")}</Label>
          <Input
            id={`${ids}-deadline`}
            type="date"
            value={form.deadline}
            onChange={(event) => set("deadline", event.target.value)}
            aria-invalid={lacking("deadline") || undefined}
            aria-describedby={lacking("deadline") ? `${ids}-deadline-error` : undefined}
            data-testid="card-deadline"
          />
          {lacking("deadline") ? <Lacking id={`${ids}-deadline-error`} text={t("needed")} /> : null}
        </div>
      </div>
    </fieldset>
  );
}
