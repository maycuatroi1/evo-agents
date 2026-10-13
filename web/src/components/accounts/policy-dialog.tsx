"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";

import { FormDialog, NumberField } from "./form-parts";
import { type Failure, useFailure, usePutPolicy } from "./hooks";
import { COOLDOWN_MINUTES, type PolicyForm, policyBody, policyFormOf, policyProblems, type PolicyRow, type Problem } from "./model";
import { MAX_FAILOVERS, STRATEGIES, type Strategy } from "./queries";

/** The visitor's policy for one project, set whole: strategy, default cooldown, failover and how many at most. */
export function PolicyDialog({ policy, open, onOpenChange }: { policy: PolicyRow | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  if (!policy) return null;
  return <PolicyFormDialog key={`${policy.project}-${String(open)}`} policy={policy} open={open} onOpenChange={onOpenChange} />;
}

function PolicyFormDialog({ policy, open, onOpenChange }: { policy: PolicyRow; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("accounts.policyForm");
  const tPolicy = useTranslations("accounts.policies");
  const tProblem = useTranslations("accounts.form.problems");
  const ids = useId();
  const write = usePutPolicy();
  const failureOf = useFailure();
  const [form, setForm] = useState<PolicyForm>(() => policyFormOf(policy));
  const [sent, setSent] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const problems = sent ? policyProblems(form) : {};
  const say = (problem: Problem | undefined, min: number, max: number) =>
    problem === "whole" ? tProblem("whole") : problem === "range" ? tProblem("range", { min, max }) : null;
  const project = policy.project;

  const save = async () => {
    setSent(true);
    setFailure(null);
    if (Object.keys(policyProblems(form)).length > 0) return;
    try {
      await write.mutateAsync({ project, body: policyBody(form) });
    } catch (error) {
      setFailure(failureOf(error));
      return;
    }
    onOpenChange(false);
    notify({ tone: "success", text: t("saved", { project }), description: t("savedText") });
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("title", { project })}
      description={t("description")}
      pending={write.isPending}
      failure={failure}
      labels={{ save: t("save"), saving: t("saving"), cancel: t("cancel") }}
      onSubmit={() => void save()}
      testId="policy-dialog"
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-strategy`}>{t("strategy")}</Label>
        <NativeSelect
          id={`${ids}-strategy`}
          value={form.strategy}
          onChange={(event) => setForm((current) => ({ ...current, strategy: event.target.value as Strategy }))}
          aria-describedby={`${ids}-strategy-hint`}
          className="w-full sm:w-64"
          data-testid="policy-strategy"
        >
          {STRATEGIES.map((strategy) => (
            <NativeSelectOption key={strategy} value={strategy}>
              {tPolicy(`strategy.${strategy}`)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <p id={`${ids}-strategy-hint`} className="text-xs text-pretty text-muted-foreground">
          {tPolicy(`strategyHint.${form.strategy}`)}
        </p>
      </div>
      <NumberField
        id={`${ids}-cooldown`}
        label={t("cooldown")}
        hint={t("cooldownHint", COOLDOWN_MINUTES)}
        problem={say(problems.cooldownMinutes, COOLDOWN_MINUTES.min, COOLDOWN_MINUTES.max)}
        value={form.cooldownMinutes}
        onChange={(cooldownMinutes) => setForm((current) => ({ ...current, cooldownMinutes }))}
        testId="policy-cooldown"
      />
      <div className="flex items-start gap-3">
        <Switch
          id={`${ids}-failover`}
          checked={form.failover}
          onCheckedChange={(failover) => setForm((current) => ({ ...current, failover }))}
          aria-describedby={`${ids}-failover-hint`}
          className="mt-0.5"
          data-testid="policy-failover"
        />
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${ids}-failover`}>{t("failover")}</Label>
          <p id={`${ids}-failover-hint`} className="text-xs text-pretty text-muted-foreground">
            {t("failoverHint")}
          </p>
        </div>
      </div>
      {form.failover ? (
        <NumberField
          id={`${ids}-max`}
          label={t("maxFailovers")}
          hint={t("maxFailoversHint", { max: MAX_FAILOVERS })}
          problem={say(problems.maxFailovers, 0, MAX_FAILOVERS)}
          value={form.maxFailovers}
          onChange={(maxFailovers) => setForm((current) => ({ ...current, maxFailovers }))}
          testId="policy-max-failovers"
        />
      ) : null}
    </FormDialog>
  );
}
