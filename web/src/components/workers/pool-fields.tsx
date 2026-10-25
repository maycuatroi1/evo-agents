"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import { projectsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { MAX_ACCEPTED, MAX_POOL_LABELS, type PoolErrors, type PoolField, type PoolForm, type PoolProblem, REAP_DAYS } from "./pool-model";
import { MAX_SLOTS } from "./queries";

function writable(project: Project): boolean {
  return project.role === "writer" || project.role === "admin";
}

/** The words of a problem of the pool form. */
export function usePoolProblem() {
  const t = useTranslations("workers.pools.form.problems");
  return (problem: PoolProblem | undefined): string | undefined => {
    if (!problem) return undefined;
    switch (problem.code) {
      case "number":
        return t("number", { min: problem.min, max: problem.max });
      case "invalid":
        return t("invalid", { values: problem.values });
      case "reserved":
        return t("reserved", { values: problem.values });
      case "tooMany":
        return t("tooMany", { max: problem.max });
      default:
        return t(problem.code);
    }
  };
}

function FieldError({ id, text }: { id: string; text?: string }) {
  if (!text) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger">
      {text}
    </p>
  );
}

function TextField({
  ids,
  field,
  label,
  hint,
  error,
  className,
  ...input
}: {
  ids: string;
  field: PoolField;
  label: string;
  hint: ReactNode;
  error?: string;
  className?: string;
} & Omit<React.ComponentProps<typeof Input>, "id" | "className">) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <Label htmlFor={`${ids}-${field}`}>{label}</Label>
      <Input
        id={`${ids}-${field}`}
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        className="h-9 font-mono placeholder:font-sans"
        aria-invalid={error ? true : undefined}
        aria-describedby={[`${ids}-${field}-hint`, error ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ")}
        data-testid={`pool-${field}`}
        {...input}
      />
      <p id={`${ids}-${field}-hint`} className="text-xs text-pretty text-muted-foreground">
        {hint}
      </p>
      <FieldError id={`${ids}-${field}-error`} text={error} />
    </div>
  );
}

/**
 * The fields of a pool: its name (only when it is made: a pool keeps its name, which starts its nodes' names), the
 * projects its nodes take runs of, the slots of each node, the labels and taints every node gets, the doctor findings a
 * node may have and still be ready, and the days an offline node stays before the hub revokes it. The projects offered
 * are those where the visitor holds the writer role, and those the pool serves already.
 */
export function PoolFields({
  ids,
  form,
  errors,
  set,
  naming,
  kept = [],
}: {
  ids: string;
  form: PoolForm;
  errors: PoolErrors;
  set: <F extends PoolField>(field: F, value: PoolForm[F]) => void;
  naming: boolean;
  /** The projects the pool serves now, offered whatever the visitor's role on them. */
  kept?: readonly string[];
}) {
  const t = useTranslations("workers.pools.form");
  const problem = usePoolProblem();
  const projects = useQuery(projectsQuery(browserApi));
  const choices = [...new Set([...kept, ...(projects.data ?? []).filter(writable).map((project) => project.name)])].sort();
  const text = (field: PoolField) => problem(errors[field]);

  return (
    <div className="grid min-w-0 gap-5 md:grid-cols-2">
      {naming ? (
        <TextField
          ids={ids}
          field="name"
          label={t("name")}
          hint={t("nameHint")}
          error={text("name")}
          value={form.name}
          onChange={(event) => set("name", event.target.value)}
          placeholder={t("namePlaceholder")}
          maxLength={40}
          aria-required="true"
          className="md:col-span-2"
        />
      ) : null}
      <fieldset className="flex min-w-0 flex-col gap-2 md:col-span-2" aria-describedby={`${ids}-projects-hint`} data-testid="pool-projects">
        <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
        {projects.isPending && kept.length === 0 ? (
          <div className="grid gap-2 sm:grid-cols-2" aria-hidden="true">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : projects.isError && choices.length === 0 ? (
          <p className="text-sm text-danger" role="alert">
            {t("projectsFailed")}
          </p>
        ) : choices.length === 0 ? (
          <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="pool-no-projects">
            {t("noProjects")}
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            {choices.map((name, index) => {
              const checked = form.projects.includes(name);
              return (
                <label
                  key={name}
                  htmlFor={`${ids}-project-${index}`}
                  className={cn(
                    "flex min-h-10 cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 transition-colors hover:bg-muted/50",
                    checked && "border-brand/40 bg-surface-selected",
                  )}
                >
                  <input
                    id={`${ids}-project-${index}`}
                    type="checkbox"
                    name="projects"
                    value={name}
                    checked={checked}
                    onChange={(event) =>
                      set("projects", event.target.checked ? [...form.projects, name].sort() : form.projects.filter((item) => item !== name))
                    }
                    className="size-4 shrink-0 cursor-pointer accent-primary"
                    aria-invalid={errors.projects ? true : undefined}
                  />
                  <span className="min-w-0 font-mono text-sm [overflow-wrap:anywhere]">{name}</span>
                </label>
              );
            })}
          </div>
        )}
        <p id={`${ids}-projects-hint`} className="text-xs text-muted-foreground">
          {t("projectsHint")}
        </p>
        <FieldError id={`${ids}-projects-error`} text={text("projects")} />
      </fieldset>
      <TextField
        ids={ids}
        field="slots"
        label={t("slots")}
        hint={t("slotsHint", { max: MAX_SLOTS })}
        error={text("slots")}
        value={form.slots}
        onChange={(event) => set("slots", event.target.value)}
        type="number"
        inputMode="numeric"
        min={1}
        max={MAX_SLOTS}
        step={1}
      />
      <TextField
        ids={ids}
        field="reapDays"
        label={t("reapDays")}
        hint={t("reapDaysHint", { min: REAP_DAYS.min, max: REAP_DAYS.max })}
        error={text("reapDays")}
        value={form.reapDays}
        onChange={(event) => set("reapDays", event.target.value)}
        type="number"
        inputMode="numeric"
        min={REAP_DAYS.min}
        max={REAP_DAYS.max}
        step={1}
      />
      <TextField
        ids={ids}
        field="labels"
        label={t("labels")}
        hint={t("labelsHint", { max: MAX_POOL_LABELS })}
        error={text("labels")}
        value={form.labels}
        onChange={(event) => set("labels", event.target.value)}
        placeholder={t("labelsPlaceholder")}
        maxLength={1600}
      />
      <TextField
        ids={ids}
        field="taints"
        label={t("taints")}
        hint={t("taintsHint")}
        error={text("taints")}
        value={form.taints}
        onChange={(event) => set("taints", event.target.value)}
        placeholder={t("taintsPlaceholder")}
        maxLength={1600}
      />
      <TextField
        ids={ids}
        field="accepted"
        label={t("accepted")}
        hint={t("acceptedHint", { max: MAX_ACCEPTED })}
        error={text("accepted")}
        value={form.accepted}
        onChange={(event) => set("accepted", event.target.value)}
        placeholder={t("acceptedPlaceholder")}
        maxLength={2100}
        className="md:col-span-2"
      />
    </div>
  );
}
