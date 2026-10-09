"use client";

import { useQuery } from "@tanstack/react-query";
import { Plus, Save, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useMemo, useState, useSyncExternalStore } from "react";

import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { type Worker, workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { plansQuery } from "@/lib/plan-queries";
import { cn } from "@/lib/utils";

import { useCuratorFailure, useWriteCharter } from "./hooks";
import {
  CHARTER_FIELDS,
  type CharterErrors,
  type CharterField,
  type CharterForm,
  type CharterProblem,
  charterBody,
  charterForm,
  LIMITS,
  ownTimeZone,
  type RoleForm,
  RUNTIMES,
  timeZones,
} from "./model";
import { TEXT_LINK } from "./parts";
import { type Charter, curatorHref } from "./queries";

/** The browser's own time zone once the page has hydrated; null on the server, so both render the same form. */
function useOwnTimeZone(): string | null {
  return useSyncExternalStore(
    () => () => {},
    () => ownTimeZone(),
    () => null,
  );
}

/** The visitor's workers that may take the night shift's runs: their own, live, serving the project, taking runs from anywhere. */
export function dutyWorkers(workers: readonly Worker[], login: string | null, project: string): Worker[] {
  return workers.filter(
    (worker) =>
      worker.owner === login && worker.status !== "revoked" && worker.projects.includes(project) && worker.dispatch_from !== "web",
  );
}

const fieldId = (base: string, field: CharterField) => `${base}-${field}`;

/** A field's problem in words. */
function useProblemText() {
  const t = useTranslations("curator.charter.errors");
  return (problem: CharterProblem) => {
    switch (problem.code) {
      case "money":
        return t("money", { max: problem.max });
      case "whole":
        return t("whole", { min: problem.min, max: problem.max });
      case "goalId":
        return t("goalId", { line: problem.line });
      case "goalWhat":
        return t("goalWhat", { line: problem.line, max: problem.max });
      case "goalRepeated":
        return t("goalRepeated", { id: problem.id });
      case "tooMany":
        return t("tooMany", { max: problem.max });
      case "planId":
        return t("planId", { line: problem.line });
      case "lineTooLong":
        return t("lineTooLong", { line: problem.line, max: problem.max });
      case "control":
        return t("control", { line: problem.line });
      case "model":
        return t("model", { max: problem.max });
      default:
        return t(problem.code);
    }
  };
}

type FieldProps = {
  base: string;
  field: CharterField;
  label: string;
  hint?: ReactNode;
  errors: CharterErrors;
  className?: string;
  children: (props: { id: string; "aria-invalid"?: true; "aria-describedby"?: string }) => ReactNode;
};

/** A label over its control, then its hint or, once the form was sent, its problem in `danger`. */
function Field({ base, field, label, hint, errors, className, children }: FieldProps) {
  const text = useProblemText();
  const id = fieldId(base, field);
  const problem = errors[field];
  const described = problem || hint ? `${id}-hint` : undefined;
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)} data-field={field}>
      <Label htmlFor={id}>{label}</Label>
      {children({ id, "aria-invalid": problem ? true : undefined, "aria-describedby": described })}
      {problem ? (
        <p id={`${id}-hint`} className="text-xs text-danger" data-testid={`charter-error-${field}`}>
          {text(problem)}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-xs text-fg-subtle">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/** A group of fields as a card: its legend as the card's head. */
function Group({ title, description, children, testId }: { title: string; description?: string; children: ReactNode; testId: string }) {
  return (
    <fieldset className="min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <legend className="sr-only">{title}</legend>
      <div className="flex min-h-12 flex-col justify-center gap-0.5 border-b px-4 py-2" aria-hidden="true">
        <span className="text-[15px] leading-[22px] font-semibold">{title}</span>
        {description ? <span className="text-xs text-fg-subtle">{description}</span> : null}
      </div>
      <div className="grid gap-4 px-4 py-4 md:grid-cols-2">{children}</div>
    </fieldset>
  );
}

function RoleFields({
  base,
  role,
  value,
  onChange,
  errors,
}: {
  base: string;
  role: "reviewer" | "builder" | "judge";
  value: RoleForm;
  onChange: (next: RoleForm) => void;
  errors: CharterErrors;
}) {
  const t = useTranslations("curator.charter");
  const tRuntime = useTranslations("runs.runtime");
  const selectId = `${base}-${role}-runtime`;
  return (
    <div className="grid min-w-0 gap-3 rounded-sm border px-3 py-3 md:col-span-2 md:grid-cols-2">
      <p className="text-[13px] font-medium md:col-span-2">{t(`roles.${role}`)}</p>
      <div className="flex min-w-0 flex-col gap-1.5">
        <Label htmlFor={selectId}>{t("fields.runtime")}</Label>
        <NativeSelect
          id={selectId}
          value={value.runtime}
          onChange={(event) => onChange({ ...value, runtime: event.target.value as RoleForm["runtime"] })}
          className="w-full"
          data-testid={`charter-${role}-runtime`}
        >
          {RUNTIMES.map((runtime) => (
            <NativeSelectOption key={runtime} value={runtime}>
              {tRuntime(runtime)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <Field base={base} field={`${role}Model`} label={t("fields.model")} hint={t("hints.model")} errors={errors}>
        {(props) => (
          <Input
            {...props}
            value={value.model}
            onChange={(event) => onChange({ ...value, model: event.target.value })}
            placeholder={t("placeholders.model")}
            className="font-mono"
            data-testid={`charter-${role}-model`}
          />
        )}
      </Field>
    </div>
  );
}

/**
 * The charter as a form, for an admin of the project: its goals, its window and the worker on duty, its budget and
 * caps, the plans the night may run and what it may change, the review, and the runtimes of the three roles. Every
 * field repeats the hub's limits, so nothing goes out while one is wrong, and the first wrong field takes focus. Saving
 * writes a new revision (the hub keeps every one); a body equal to the newest revision writes none. A refusal of the
 * hub (a worker that is not one of the writer's own, a time zone Postgres does not know) shows above the buttons.
 */
export function CharterFormView({ project, charter, login }: { project: string; charter: Charter | null; login: string | null }) {
  const t = useTranslations("curator.charter");
  const base = useId();
  const router = useRouter();
  const zone = useOwnTimeZone();
  const workers = useQuery(workersQuery(browserApi));
  const plans = useQuery(plansQuery(browserApi, project));
  const mutation = useWriteCharter(project);
  const failure = useCuratorFailure();
  const eligible = useMemo(() => dutyWorkers(workers.data ?? [], login, project), [workers.data, login, project]);
  const [form, setForm] = useState<CharterForm>(() => charterForm(charter, { worker: "", timezone: "" }));
  const [errors, setErrors] = useState<CharterErrors>({});
  const [error, setError] = useState<WriteFailure | null>(null);
  const zones = useMemo(() => (zone === null ? [] : timeZones()), [zone]);
  const activePlans = (plans.data ?? []).filter((plan) => plan.area === "active").map((plan) => plan.plan_id);

  // A first charter: the visitor's own time zone and their first worker that may take the night's runs, until typed.
  const resolved: CharterForm = {
    ...form,
    timezone: form.timezone || zone || "UTC",
    worker: form.worker || eligible[0]?.name || "",
  };
  const set = <K extends keyof CharterForm>(key: K, value: CharterForm[K]) => setForm((current) => ({ ...current, [key]: value }));
  const workerOptions = [...new Set([...(charter ? [charter.worker] : []), ...eligible.map((worker) => worker.name)])];

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (mutation.isPending) return;
    const result = charterBody(resolved);
    if (result.errors) {
      setErrors(result.errors);
      const first = CHARTER_FIELDS.find((field) => result.errors[field]);
      if (first) {
        const target = document.getElementById(fieldId(base, first === "goals" ? "goals" : first));
        target?.focus();
      }
      return;
    }
    setErrors({});
    setError(null);
    mutation.mutate(result.body, {
      onSuccess: (saved) => {
        notify({
          tone: "success",
          text: charter && saved.revision === charter.revision ? t("unchanged.title") : t("saved.title", { revision: saved.revision }),
          description: charter && saved.revision === charter.revision ? t("unchanged.text") : t("saved.text"),
        });
        router.push(curatorHref(project, "charter"));
      },
      onError: (failed) => setError(failure(failed, "charter")),
    });
  };

  const goalsError = errors.goals;
  const text = useProblemText();
  return (
    <form noValidate onSubmit={submit} className="flex flex-col gap-4" aria-label={t("formLabel")} data-testid="charter-form">
      <Group title={t("groups.goals")} description={t("groupHints.goals")} testId="charter-goals">
        <div className="flex min-w-0 flex-col gap-3 md:col-span-2">
          {form.goals.length === 0 ? <p className="text-[13px] text-muted-foreground">{t("noGoals")}</p> : null}
          <ol className="flex flex-col gap-3">
            {form.goals.map((goal, index) => (
              <li key={index} className="grid min-w-0 gap-2 rounded-sm border px-3 py-3 sm:grid-cols-[12rem_minmax(0,1fr)_auto] sm:items-start">
                <div className="flex min-w-0 flex-col gap-1.5">
                  <Label htmlFor={index === 0 ? fieldId(base, "goals") : `${base}-goal-${index}-id`}>{t("fields.goalId", { n: index + 1 })}</Label>
                  <Input
                    id={index === 0 ? fieldId(base, "goals") : `${base}-goal-${index}-id`}
                    value={goal.id}
                    onChange={(event) => set("goals", form.goals.map((item, at) => (at === index ? { ...item, id: event.target.value } : item)))}
                    className="font-mono"
                    placeholder="night-shift"
                    data-testid="charter-goal-id"
                  />
                </div>
                <div className="flex min-w-0 flex-col gap-1.5">
                  <Label htmlFor={`${base}-goal-${index}-what`}>{t("fields.goalWhat", { n: index + 1 })}</Label>
                  <Textarea
                    id={`${base}-goal-${index}-what`}
                    value={goal.what}
                    rows={2}
                    onChange={(event) => set("goals", form.goals.map((item, at) => (at === index ? { ...item, what: event.target.value } : item)))}
                    data-testid="charter-goal-what"
                  />
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="sm:mt-6"
                  aria-label={t("removeGoal", { n: index + 1 })}
                  onClick={() => set("goals", form.goals.filter((_, at) => at !== index))}
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ol>
          {goalsError ? (
            <p className="text-xs text-danger" role="alert" data-testid="charter-error-goals">
              {text(goalsError)}
            </p>
          ) : null}
          <Button
            type="button"
            variant="secondary"
            className="w-fit"
            disabled={form.goals.length >= LIMITS.goals}
            onClick={() => set("goals", [...form.goals, { id: "", what: "" }])}
            data-testid="charter-add-goal"
          >
            <Plus aria-hidden="true" />
            {t("addGoal")}
          </Button>
        </div>
      </Group>

      <Group title={t("groups.window")} description={t("groupHints.window")} testId="charter-window">
        <Field base={base} field="windowStart" label={t("fields.windowStart")} errors={errors}>
          {(props) => <Input {...props} type="time" step={60} value={form.windowStart} onChange={(event) => set("windowStart", event.target.value)} data-testid="charter-window-start" />}
        </Field>
        <Field base={base} field="windowEnd" label={t("fields.windowEnd")} hint={t("hints.windowEnd")} errors={errors}>
          {(props) => <Input {...props} type="time" step={60} value={form.windowEnd} onChange={(event) => set("windowEnd", event.target.value)} data-testid="charter-window-end" />}
        </Field>
        <Field base={base} field="timezone" label={t("fields.timezone")} hint={t("hints.timezone")} errors={errors}>
          {(props) => (
            <>
              <Input
                {...props}
                list={`${base}-zones`}
                value={resolved.timezone}
                onChange={(event) => set("timezone", event.target.value)}
                autoComplete="off"
                spellCheck={false}
                data-testid="charter-timezone"
              />
              <datalist id={`${base}-zones`}>
                {zones.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </>
          )}
        </Field>
        <Field base={base} field="briefAt" label={t("fields.briefAt")} hint={t("hints.briefAt")} errors={errors}>
          {(props) => <Input {...props} type="time" step={60} value={form.briefAt} onChange={(event) => set("briefAt", event.target.value)} data-testid="charter-brief-at" />}
        </Field>
        <Field
          base={base}
          field="worker"
          label={t("fields.worker")}
          className="md:col-span-2"
          hint={
            workerOptions.length === 0 && !workers.isPending
              ? t.rich("hints.noWorker", { link: (chunks) => <Link href="/workers" className={TEXT_LINK}>{chunks}</Link> })
              : t("hints.worker")
          }
          errors={errors}
        >
          {(props) => (
            <NativeSelect
              {...props}
              value={resolved.worker}
              onChange={(event) => set("worker", event.target.value)}
              className="w-full md:w-80"
              disabled={workerOptions.length === 0}
              data-testid="charter-worker"
            >
              {workerOptions.length === 0 ? <NativeSelectOption value="">{t("noWorkerOption")}</NativeSelectOption> : null}
              {workerOptions.map((name) => (
                <NativeSelectOption key={name} value={name}>
                  {name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </Field>
      </Group>

      <Group title={t("groups.budget")} description={t("groupHints.budget")} testId="charter-budget">
        <Field base={base} field="nightBudget" label={t("fields.nightBudget")} hint={t("hints.usd", { max: LIMITS.budgetUsd })} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="decimal" min={0} step="0.01" value={form.nightBudget} onChange={(event) => set("nightBudget", event.target.value)} data-testid="charter-night-budget" />}
        </Field>
        <Field base={base} field="runBudget" label={t("fields.runBudget")} hint={t("hints.runBudget")} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="decimal" min={0} step="0.01" value={form.runBudget} onChange={(event) => set("runBudget", event.target.value)} data-testid="charter-run-budget" />}
        </Field>
        <Field base={base} field="maxRunsPerNight" label={t("fields.maxRunsPerNight")} hint={t("hints.range", LIMITS.runsPerNight)} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.maxRunsPerNight} onChange={(event) => set("maxRunsPerNight", event.target.value)} data-testid="charter-max-runs" />}
        </Field>
        <Field base={base} field="runMaxTurns" label={t("fields.runMaxTurns")} hint={t("hints.range", LIMITS.turns)} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.runMaxTurns} onChange={(event) => set("runMaxTurns", event.target.value)} data-testid="charter-max-turns" />}
        </Field>
        <Field base={base} field="runMinutes" label={t("fields.runMinutes")} hint={t("hints.range", LIMITS.runMinutes)} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.runMinutes} onChange={(event) => set("runMinutes", event.target.value)} data-testid="charter-run-minutes" />}
        </Field>
        <Field base={base} field="maxFailedInARow" label={t("fields.maxFailedInARow")} hint={t("hints.circuit")} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.maxFailedInARow} onChange={(event) => set("maxFailedInARow", event.target.value)} data-testid="charter-circuit" />}
        </Field>
      </Group>

      <Group title={t("groups.scope")} description={t("groupHints.scope")} testId="charter-scope">
        <Field
          base={base}
          field="nightPlans"
          label={t("fields.nightPlans")}
          className="md:col-span-2"
          hint={activePlans.length > 0 ? t("hints.nightPlansKnown", { plans: activePlans.join(", ") }) : t("hints.nightPlans")}
          errors={errors}
        >
          {(props) => (
            <Textarea {...props} rows={3} value={form.nightPlans} onChange={(event) => set("nightPlans", event.target.value)} className="font-mono" spellCheck={false} data-testid="charter-night-plans" />
          )}
        </Field>
        <Field base={base} field="maxDecisionsPerDay" label={t("fields.maxDecisionsPerDay")} hint={t("hints.decisions")} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.maxDecisionsPerDay} onChange={(event) => set("maxDecisionsPerDay", event.target.value)} data-testid="charter-max-decisions" />}
        </Field>
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="flex items-center gap-3 pt-6 max-md:pt-0">
            <Switch id={`${base}-auto-merge`} checked={form.autoMerge} onCheckedChange={(checked) => set("autoMerge", checked)} aria-describedby={`${base}-auto-merge-hint`} data-testid="charter-auto-merge" />
            <Label htmlFor={`${base}-auto-merge`}>{t("fields.autoMerge")}</Label>
          </div>
          <p id={`${base}-auto-merge-hint`} className="text-xs text-fg-subtle">
            {t("hints.autoMerge")}
          </p>
        </div>
        <Field base={base} field="outcomeDays" label={t("fields.outcomeDays")} className="md:col-span-2" hint={t("hints.outcomeDays")} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.outcomeDays} onChange={(event) => set("outcomeDays", event.target.value)} className="md:max-w-40" data-testid="charter-outcome-days" />}
        </Field>
        <Field base={base} field="protectedPaths" label={t("fields.protectedPaths")} className="md:col-span-2" hint={t("hints.protectedPaths")} errors={errors}>
          {(props) => (
            <Textarea {...props} rows={4} value={form.protectedPaths} onChange={(event) => set("protectedPaths", event.target.value)} className="font-mono" spellCheck={false} data-testid="charter-protected-paths" />
          )}
        </Field>
      </Group>

      <Group title={t("groups.review")} description={t("groupHints.review")} testId="charter-review">
        <Field base={base} field="reviewLenses" label={t("fields.reviewLenses")} hint={t("hints.range", LIMITS.lenses)} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.reviewLenses} onChange={(event) => set("reviewLenses", event.target.value)} data-testid="charter-review-lenses" />}
        </Field>
        <Field base={base} field="reviewDays" label={t("fields.reviewDays")} hint={t("hints.range", LIMITS.reviewDays)} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="numeric" value={form.reviewDays} onChange={(event) => set("reviewDays", event.target.value)} data-testid="charter-review-days" />}
        </Field>
        <Field base={base} field="reviewBudget" label={t("fields.reviewBudget")} hint={t("hints.reviewBudget")} errors={errors}>
          {(props) => <Input {...props} type="number" inputMode="decimal" min={0} step="0.01" value={form.reviewBudget} onChange={(event) => set("reviewBudget", event.target.value)} data-testid="charter-review-budget" />}
        </Field>
      </Group>

      <Group title={t("groups.roles")} description={t("groupHints.roles")} testId="charter-roles">
        <RoleFields base={base} role="reviewer" value={form.reviewer} onChange={(next) => set("reviewer", next)} errors={errors} />
        <RoleFields base={base} role="builder" value={form.builder} onChange={(next) => set("builder", next)} errors={errors} />
        <RoleFields base={base} role="judge" value={form.judge} onChange={(next) => set("judge", next)} errors={errors} />
        {form.hiddenChecks !== null ? (
          <Field base={base} field="hiddenChecks" label={t("fields.hiddenChecks")} className="md:col-span-2" hint={t("hints.hiddenChecks")} errors={errors}>
            {(props) => (
              <Textarea {...props} rows={3} value={form.hiddenChecks ?? ""} onChange={(event) => set("hiddenChecks", event.target.value)} className="font-mono" spellCheck={false} data-testid="charter-hidden-checks" />
            )}
          </Field>
        ) : null}
      </Group>

      {Object.keys(errors).length > 0 ? (
        <p role="alert" className="text-sm text-danger" data-testid="charter-form-problems">
          {t("problems", { count: Object.keys(errors).length })}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-4">
        <Button asChild variant="outline">
          <Link href={curatorHref(project, charter ? "charter" : "")} data-testid="charter-cancel">
            {t("cancel")}
          </Link>
        </Button>
        <Button type="submit" busy={mutation.isPending} data-testid="charter-save">
          <Save aria-hidden="true" />
          {mutation.isPending ? t("saving") : charter ? t("save") : t("create")}
        </Button>
      </div>
    </form>
  );
}
