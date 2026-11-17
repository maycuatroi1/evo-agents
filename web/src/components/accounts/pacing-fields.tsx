"use client";

import { useTranslations } from "next-intl";

import { NumberField } from "./form-parts";
import { CEILING_PCT, type Problem, RESERVE_PCT } from "./model";

export type PacingValues = { reservePct: string; ceilingPct: string };

/**
 * The two thresholds of quota-pacing (docs/credentials.md, section "Accounts"): the share of a weekly window an account
 * keeps back while another has more, and the five-hour use above which it waits while a cooler one is ready. The
 * policy dialog and a pool's policy both show them while their strategy is quota-pacing.
 */
export function PacingFields({
  id,
  values,
  problems,
  onChange,
}: {
  id: string;
  values: PacingValues;
  problems: Partial<Record<keyof PacingValues, Problem>>;
  onChange: (values: Partial<PacingValues>) => void;
}) {
  const t = useTranslations("accounts.pacing");
  const tProblem = useTranslations("accounts.form.problems");
  const say = (problem: Problem | undefined, bounds: { min: number; max: number }) =>
    problem === "whole" ? tProblem("whole") : problem === "range" ? tProblem("range", bounds) : null;
  return (
    <div className="flex flex-col gap-5" data-testid="pacing-fields">
      <NumberField
        id={`${id}-reserve`}
        label={t("reserve")}
        hint={t("reserveHint", RESERVE_PCT)}
        problem={say(problems.reservePct, RESERVE_PCT)}
        value={values.reservePct}
        onChange={(reservePct) => onChange({ reservePct })}
        testId="pacing-reserve"
      />
      <NumberField
        id={`${id}-ceiling`}
        label={t("ceiling")}
        hint={t("ceilingHint", CEILING_PCT)}
        problem={say(problems.ceilingPct, CEILING_PCT)}
        value={values.ceilingPct}
        onChange={(ceilingPct) => onChange({ ceilingPct })}
        testId="pacing-ceiling"
      />
    </div>
  );
}
