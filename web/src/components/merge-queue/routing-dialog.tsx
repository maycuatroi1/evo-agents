"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { type AutonomyPolicy, mergeKeys, type PolicyWrite, writePolicy } from "@/lib/merge-queue";
import { runnableModelsQuery } from "@/lib/runnable-models";

import {
  LEVELS,
  modelSuggestions,
  readRouting,
  ROUTING_STRATEGIES,
  type RoutingForm,
  routingFormOf,
  type RoutingProblem,
  routingProblems,
  routingSection,
  type RoutingStrategy,
  withRouting,
} from "./routing-model";

type T = ReturnType<typeof useTranslations<"policy.routing">>;

function sayProblem(t: T, problem: RoutingProblem | undefined): string | null {
  if (!problem) return null;
  if (problem.code === "required") return t("problems.required");
  if (problem.code === "model") return t("problems.model", { max: problem.max });
  return t(`problems.${problem.code}`, { min: problem.min, max: problem.max });
}

/** One text field of the form: a percent, or a model with the suggestions of the datalist `list`. */
function Field({
  id,
  label,
  hint,
  value,
  problem,
  onChange,
  list,
  testId,
}: {
  id: string;
  label: string;
  hint?: string;
  value: string;
  problem: string | null;
  onChange: (value: string) => void;
  list?: string;
  testId: string;
}) {
  const described = [hint ? `${id}-hint` : null, problem ? `${id}-error` : null].filter(Boolean).join(" ");
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="text"
        inputMode={list ? undefined : "numeric"}
        list={list}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
        spellCheck={false}
        className={list ? "h-9 w-full font-mono text-[13px]" : "h-9 w-full tabular-nums sm:w-32"}
        aria-invalid={problem ? true : undefined}
        aria-describedby={described || undefined}
        data-testid={testId}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {problem ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger">
          {problem}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The Token strategy as a form, for an admin of the project: on or off, the strategy, the weekly reserve and the
 * five-hour ceiling, the scorer, and the ladder and overflow model of each difficulty, each model field suggesting the
 * runtime's own models and the gateway models the project may run. Save writes the next revision of the whole policy
 * with this section changed, at the revision shown (if_revision), so a policy changed meanwhile is never overwritten.
 */
export function RoutingDialog({ project, policy, open, onOpenChange }: { project: string; policy: AutonomyPolicy; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("policy.routing");
  const ids = useId();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const runnable = useQuery({ ...runnableModelsQuery(browserApi, project), enabled: open });
  const [form, setForm] = useState<RoutingForm>(() => routingFormOf(readRouting(policy.policy)));
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const write = useMutation({
    mutationFn: (body: PolicyWrite) => writePolicy(browserApi(), project, body),
    onError: (failed) => {
      if (isApiError(failed) && failed.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: mergeKeys.all(project) }),
  });
  const problems = sent ? routingProblems(form) : {};
  const say = (key: string) => sayProblem(t, problems[key]);
  const suggestions = modelSuggestions(runnable.data ?? []);
  const list = `${ids}-models`;
  const set = (change: Partial<RoutingForm>) => setForm((current) => ({ ...current, ...change }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (write.isPending) return;
    setSent(true);
    setError(null);
    if (Object.keys(routingProblems(form)).length > 0) return;
    const body = withRouting(policy.policy, routingSection(form, policy.policy));
    write.mutate(
      { policy: body, note: t("note"), if_revision: policy.revision },
      {
        onSuccess: (saved) => {
          onOpenChange(false);
          notify(saved.changed ? { tone: "success", text: t("saved", { revision: saved.revision }) } : { tone: "info", text: t("unchanged") });
        },
        onError: (failed) => setError(failure(failed, { 403: t("forbidden"), 409: t("conflict"), 422: t("refused") })),
      },
    );
  };

  const guard = (event: Event) => write.isPending && event.preventDefault();
  return (
    <Dialog open={open} onOpenChange={(next) => (write.isPending ? undefined : onOpenChange(next))}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-2xl" onEscapeKeyDown={guard} onInteractOutside={guard} data-testid="routing-dialog">
        <DialogHeader>
          <DialogTitle>{t("editTitle")}</DialogTitle>
          <DialogDescription className="text-pretty">{t("description")}</DialogDescription>
        </DialogHeader>
        <form noValidate onSubmit={submit} className="flex flex-col gap-5">
          <div className="flex items-start gap-3">
            <Switch id={`${ids}-enabled`} checked={form.enabled} onCheckedChange={(enabled) => set({ enabled })} className="mt-0.5" aria-describedby={`${ids}-enabled-hint`} data-testid="routing-enabled" />
            <div className="flex flex-col gap-1">
              <Label htmlFor={`${ids}-enabled`}>{t("enabled")}</Label>
              <p id={`${ids}-enabled-hint`} className="text-xs text-pretty text-muted-foreground">
                {t("enabledHint")}
              </p>
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-strategy`}>{t("strategy")}</Label>
            <NativeSelect
              id={`${ids}-strategy`}
              value={form.strategy}
              onChange={(event) => set({ strategy: event.target.value as RoutingStrategy })}
              aria-describedby={`${ids}-strategy-hint`}
              className="w-full sm:w-64"
              data-testid="routing-strategy"
            >
              {ROUTING_STRATEGIES.map((strategy) => (
                <NativeSelectOption key={strategy} value={strategy}>
                  {t(`strategies.${strategy}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
            <p id={`${ids}-strategy-hint`} className="text-xs text-pretty text-muted-foreground">
              {t(`strategyHints.${form.strategy}`)}
            </p>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id={`${ids}-reserve`} label={t("reserve")} hint={t("reserveHint")} value={form.reservePct} problem={say("reservePct")} onChange={(reservePct) => set({ reservePct })} testId="routing-reserve" />
            <Field id={`${ids}-ceiling`} label={t("ceiling")} hint={t("ceilingHint")} value={form.ceilingPct} problem={say("ceilingPct")} onChange={(ceilingPct) => set({ ceilingPct })} testId="routing-ceiling" />
          </div>
          <Field id={`${ids}-scorer`} label={t("scorer")} hint={t("scorerHint")} value={form.scorer} problem={say("scorer")} onChange={(scorer) => set({ scorer })} list={list} testId="routing-scorer" />
          {(["ladder", "overflow"] as const).map((group) => (
            <fieldset key={group} className="grid min-w-0 gap-4 rounded-sm border px-3 py-3 sm:grid-cols-3" data-testid={`routing-${group}`}>
              <legend className="px-1 text-sm font-medium">{t(`${group}.title`)}</legend>
              <p className="-mt-1 text-xs text-pretty text-muted-foreground sm:col-span-3">{t(`${group}.hint`)}</p>
              {LEVELS.map((level) => (
                <Field
                  key={level}
                  id={`${ids}-${group}-${level}`}
                  label={t(`levels.${level}`)}
                  value={form[group][level]}
                  problem={say(`${group}.${level}`)}
                  onChange={(value) => setForm((current) => ({ ...current, [group]: { ...current[group], [level]: value } }))}
                  list={list}
                  testId={`routing-${group}-${level}`}
                />
              ))}
            </fieldset>
          ))}
          <datalist id={list}>
            {suggestions.map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" size="lg" disabled={write.isPending}>
                {t("cancel")}
              </Button>
            </DialogClose>
            <Button type="submit" size="lg" busy={write.isPending} data-testid="routing-save">
              {write.isPending ? t("saving") : t("save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
