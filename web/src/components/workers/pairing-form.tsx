"use client";

import { KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { DialogClose, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import { type useWorkerWrite } from "./hooks";
import { isWorkerName, parseLabels } from "./model";
import { MAX_LABELS, MAX_SLOTS, MIN_SLOTS, type Pairing, type PairingRequest } from "./queries";
import { FullPermissions } from "./register-parts";

export type Write = ReturnType<typeof useWorkerWrite<PairingRequest, Pairing>>;

export type Form = { name: string; projects: string[]; slots: string; labels: string; terminal: boolean };
type Errors = Partial<Record<"name" | "projects" | "slots" | "labels", string>>;

function FieldError({ id, text }: { id: string; text?: string }) {
  if (!text) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger">
      {text}
    </p>
  );
}

export function PairingForm({
  write,
  form,
  choices,
  loadingProjects,
  projectsFailed,
  onChange,
  onCreated,
  alert,
}: {
  write: Write;
  form: Form;
  choices: string[];
  loadingProjects: boolean;
  projectsFailed: boolean;
  onChange: (form: Form) => void;
  onCreated: (pairing: Pairing) => void;
  /** What the hub refused when the code was asked for, shown above the buttons. */
  alert: ReactNode;
}) {
  const t = useTranslations("workers.register");
  const ids = useId();
  const [errors, setErrors] = useState<Errors>({});
  const pending = write.isPending;
  const noProjects = !loadingProjects && !projectsFailed && choices.length === 0;

  const describedBy = (field: keyof Errors, hint = true) =>
    [hint ? `${ids}-${field}-hint` : "", errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ") || undefined;
  const set = (patch: Partial<Form>, field?: keyof Errors) => {
    onChange({ ...form, ...patch });
    if (field) setErrors((current) => ({ ...current, [field]: undefined }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const found: Errors = {};
    const name = form.name.trim();
    if (!name) found.name = t("nameRequired");
    else if (!isWorkerName(name)) found.name = t("nameInvalid");
    if (form.projects.length === 0) found.projects = t("projectsRequired");
    const slots = Number(form.slots);
    if (!Number.isInteger(slots) || slots < MIN_SLOTS || slots > MAX_SLOTS) found.slots = t("slotsInvalid", { min: MIN_SLOTS, max: MAX_SLOTS });
    const labels = parseLabels(form.labels);
    if (labels.invalid.length) found.labels = t("labelsInvalid", { labels: labels.invalid.join(", ") });
    else if (labels.tooMany) found.labels = t("labelsTooMany", { max: MAX_LABELS });
    setErrors(found);
    const first = (["name", "projects", "slots", "labels"] as const).find((key) => found[key]);
    if (first) {
      document.getElementById(first === "projects" ? `${ids}-project-0` : `${ids}-${first}`)?.focus();
      return;
    }
    let created: Pairing;
    try {
      created = await write.mutateAsync({
        name,
        projects: form.projects,
        slots,
        labels: labels.labels,
        allow_web_terminal: form.terminal,
      });
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    onCreated(created);
  };

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5" aria-busy={pending || undefined}>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
        <Input
          id={`${ids}-name`}
          name="name"
          value={form.name}
          onChange={(event) => set({ name: event.target.value }, "name")}
          placeholder={t("namePlaceholder")}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={100}
          className="h-9 font-mono placeholder:font-sans"
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={describedBy("name")}
          aria-required="true"
          data-testid="register-name"
        />
        <p id={`${ids}-name-hint`} className="text-xs text-muted-foreground">
          {t("nameHint")}
        </p>
        <FieldError id={`${ids}-name-error`} text={errors.name} />
      </div>

      <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={describedBy("projects")}>
        <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
        {loadingProjects ? (
          <div className="grid gap-2 sm:grid-cols-2" aria-hidden="true">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : projectsFailed ? (
          <p className="text-sm text-danger" role="alert">
            {t("projectsFailed")}
          </p>
        ) : noProjects ? (
          <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="register-no-projects">
            {t("noProjects")}
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2" data-testid="register-projects">
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
                      set(
                        {
                          projects: event.target.checked
                            ? [...form.projects, name]
                            : form.projects.filter((project) => project !== name),
                        },
                        "projects",
                      )
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
        <FieldError id={`${ids}-projects-error`} text={errors.projects} />
      </fieldset>

      <div className="grid gap-5 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-slots`}>{t("slots")}</Label>
          <Input
            id={`${ids}-slots`}
            name="slots"
            type="number"
            inputMode="numeric"
            min={MIN_SLOTS}
            max={MAX_SLOTS}
            step={1}
            value={form.slots}
            onChange={(event) => set({ slots: event.target.value }, "slots")}
            className="h-9 tabular-nums"
            aria-invalid={errors.slots ? true : undefined}
            aria-describedby={describedBy("slots")}
            data-testid="register-slots"
          />
          <p id={`${ids}-slots-hint`} className="text-xs text-muted-foreground">
            {t("slotsHint", { min: MIN_SLOTS, max: MAX_SLOTS })}
          </p>
          <FieldError id={`${ids}-slots-error`} text={errors.slots} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-labels`}>{t("labels")}</Label>
          <Input
            id={`${ids}-labels`}
            name="labels"
            value={form.labels}
            onChange={(event) => set({ labels: event.target.value }, "labels")}
            placeholder={t("labelsPlaceholder")}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={800}
            className="h-9 font-mono placeholder:font-sans"
            aria-invalid={errors.labels ? true : undefined}
            aria-describedby={describedBy("labels")}
            data-testid="register-labels"
          />
          <p id={`${ids}-labels-hint`} className="text-xs text-muted-foreground">
            {t("labelsHint")}
          </p>
          <FieldError id={`${ids}-labels-error`} text={errors.labels} />
        </div>
      </div>

      <label
        htmlFor={`${ids}-terminal`}
        className="flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 transition-colors hover:bg-muted/50"
      >
        <input
          id={`${ids}-terminal`}
          type="checkbox"
          name="allow_web_terminal"
          checked={form.terminal}
          onChange={(event) => set({ terminal: event.target.checked })}
          className="mt-0.5 size-4 shrink-0 cursor-pointer accent-primary"
          aria-describedby={`${ids}-terminal-hint`}
          data-testid="register-terminal"
        />
        <span className="flex flex-col gap-1">
          <span className="text-sm font-medium">{t("terminal")}</span>
          <span id={`${ids}-terminal-hint`} className="text-xs leading-snug text-muted-foreground">
            {t("terminalHint")}
          </span>
        </span>
      </label>

      <FullPermissions text={t("warning")} />
      {alert}
      <DialogFooter>
        <DialogClose asChild>
          <Button
            type="button"
            variant="outline"
            size="lg"
            aria-disabled={pending || undefined}
            className="aria-disabled:opacity-50"
            onClick={(event) => pending && event.preventDefault()}
          >
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" disabled={noProjects} busy={pending} data-testid="register-create">
          <KeyRound aria-hidden="true" />
          {pending ? t("creating") : t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}
