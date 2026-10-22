"use client";

import { useQuery } from "@tanstack/react-query";
import { Save, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { projectsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { FormError } from "./form-error";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import { isWorkerName, parseLabels, workerView } from "./model";
import { MAX_LABELS, MAX_SLOTS, MIN_SLOTS, type Worker } from "./queries";
import { updateWorker, type WorkerUpdate } from "./update-worker";
import { VersionLockSwitch } from "./version-lock";

/** `manifest.PLUGIN_ID` and `runs.MAX_WORKER_PLUGINS` of the API: the plugins whose runtimes a worker takes, by id. */
const PLUGIN_ID = /^[a-z][a-z0-9-]{0,62}$/;
const MAX_PLUGINS = 16;

/** Plugin ids typed as labels are: unique, in order, each checked against the API's pattern. */
export function parsePlugins(input: string): { plugins: string[]; invalid: string[]; tooMany: boolean } {
  const words = [...new Set(input.split(/[\s,]+/).map((word) => word.trim()).filter(Boolean))];
  const plugins = words.filter((word) => PLUGIN_ID.test(word));
  return { plugins, invalid: words.filter((word) => !PLUGIN_ID.test(word)), tooMany: plugins.length > MAX_PLUGINS };
}

type Form = { name: string; slots: string; labels: string; plugins: string; projects: string[] };
type Field = keyof Form;
type Errors = Partial<Record<Field, string>>;

function formOf(worker: Worker): Form {
  return {
    name: worker.name,
    slots: String(worker.slots),
    labels: worker.labels.join(", "),
    plugins: worker.plugins.join(", "),
    projects: [...worker.projects].sort(),
  };
}

function sameLabels(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((label, index) => label === b[index]);
}

function writable(project: Project): boolean {
  return project.role === "writer" || project.role === "admin";
}

/**
 * The projects the form offers: those the worker serves, which it keeps whatever role the owner holds on them now, and
 * those the owner may add, where they hold writer. While the projects are read, or when they cannot be, the worker's
 * own are offered alone.
 */
function projectChoices(worker: Worker, projects: Project[] | undefined): string[] {
  return [...new Set([...worker.projects, ...(projects ?? []).filter(writable).map((project) => project.name)])].sort();
}

/**
 * The owner's settings of a worker: its name, slots, labels, the plugins whose runtimes it takes and the projects it
 * serves, saved with PATCH /v1/workers/{id}, and under them the switch that locks its version, which saves as soon as it is flipped
 * (version-lock.tsx). The fields show what the hub holds until the owner types; a save sends only the fields that differ, and once the hub answers the page and
 * the list read the worker again, so the header, the Scope card and the Slots line show the new values. What the hub
 * refuses (a name taken, a value out of range, a project it may not add or still needs, the worker revoked meanwhile)
 * stays at the form until the next try.
 * Another member never gets here, and a hub admin who is not the owner sees the values in Scope, without this form.
 */
export function WorkerSettings({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.settings");
  const ids = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, body: WorkerUpdate) => updateWorker(api, worker.id, body));
  const projects = useQuery(projectsQuery(browserApi));
  const [edits, setEdits] = useState<Form | null>(null);
  const [errors, setErrors] = useState<Errors>({});
  const revoked = workerView(worker) === "revoked";
  const pending = write.isPending;

  // A revoked worker changes no more; the card stays only to say why the last save failed.
  if (revoked && !write.isError) return null;

  const form = edits ?? formOf(worker);
  const choices = projectChoices(worker, projects.data);
  const set = <F extends Field>(field: F, value: Form[F]) => {
    setEdits({ ...form, [field]: value });
    setErrors((current) => ({ ...current, [field]: undefined }));
    if (write.isError) write.reset();
  };
  const describedBy = (field: Field) => [`${ids}-${field}-hint`, errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || revoked) return;
    const found: Errors = {};
    const name = form.name.trim();
    if (!name) found.name = t("nameRequired");
    else if (!isWorkerName(name)) found.name = t("nameInvalid");
    const slots = Number(form.slots);
    if (form.slots.trim() === "" || !Number.isInteger(slots) || slots < MIN_SLOTS || slots > MAX_SLOTS) {
      found.slots = t("slotsInvalid", { min: MIN_SLOTS, max: MAX_SLOTS });
    }
    const labels = parseLabels(form.labels);
    if (labels.invalid.length) found.labels = t("labelsInvalid", { labels: labels.invalid.join(", ") });
    else if (labels.tooMany) found.labels = t("labelsTooMany", { max: MAX_LABELS });
    const plugins = parsePlugins(form.plugins);
    if (plugins.invalid.length) found.plugins = t("pluginsInvalid", { plugins: plugins.invalid.join(", ") });
    else if (plugins.tooMany) found.plugins = t("pluginsTooMany", { max: MAX_PLUGINS });
    if (form.projects.length === 0) found.projects = t("projectsRequired");
    setErrors(found);
    const first = (["name", "slots", "labels", "plugins", "projects"] as const).find((key) => found[key]);
    if (first) {
      document.getElementById(first === "projects" ? `${ids}-project-0` : `${ids}-${first}`)?.focus();
      return;
    }

    const body: WorkerUpdate = {};
    if (name !== worker.name) body.name = name;
    if (slots !== worker.slots) body.slots = slots;
    if (!sameLabels(labels.labels, worker.labels)) body.labels = labels.labels;
    if (!sameLabels(plugins.plugins, worker.plugins)) body.plugins = plugins.plugins;
    if (!sameLabels(form.projects, [...worker.projects].sort())) body.projects = form.projects;
    if (Object.keys(body).length === 0) {
      setEdits(null); // what was typed is what the hub holds already
      return;
    }
    let saved: Worker;
    try {
      saved = await write.mutateAsync(body); // resolves once the worker queries were reloaded
    } catch (error) {
      // a 409 about the name or about a project the worker still needs is said at its field, not twice
      if (isApiError(error) && error.status === 409 && body.name !== undefined && /worker named/i.test(error.info.message)) {
        write.reset();
        setErrors({ name: t("nameTaken", { name }) });
        document.getElementById(`${ids}-name`)?.focus();
      } else if (isApiError(error) && error.status === 409 && body.projects !== undefined && /cannot stop serving/i.test(error.info.message)) {
        write.reset();
        setErrors({ projects: t("projectsStillNeeded", { message: error.info.message }) });
        document.getElementById(`${ids}-project-0`)?.focus();
      }
      return; // anything else is said above the buttons, from the mutation's state
    }
    setEdits(null);
    notify({ tone: "success", text: t("saved", { name: saved.name }), description: t("savedText") });
  };

  const revokedMeanwhile = revoked || (isApiError(write.error) && write.error.status === 409 && /revoked/i.test(write.error.info.message));
  const error = write.isError ? failure(write.error, revokedMeanwhile ? { 409: t("revoked") } : {}) : null;
  const dirty = edits !== null && JSON.stringify(edits) !== JSON.stringify(formOf(worker));

  return (
    <Card className="min-w-0" data-testid="worker-settings">
      <CardHeader>
        <CardTitle>
          <h2 id={`${ids}-title`}>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">
        <form
          onSubmit={(event) => void submit(event)}
          noValidate
          aria-labelledby={`${ids}-title`}
          aria-busy={pending || undefined}
          className="flex flex-col gap-5"
        >
          <fieldset disabled={revoked} className="grid min-w-0 gap-5 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,2fr)]">
            <div className="flex min-w-0 flex-col gap-1.5">
              <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
              <Input
                id={`${ids}-name`}
                name="name"
                value={form.name}
                onChange={(event) => set("name", event.target.value)}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={100}
                className="h-9 font-mono"
                aria-invalid={errors.name ? true : undefined}
                aria-describedby={describedBy("name")}
                aria-required="true"
                data-testid="worker-settings-name"
              />
              <p id={`${ids}-name-hint`} className="text-xs text-muted-foreground">
                {t("nameHint")}
              </p>
              <FieldError id={`${ids}-name-error`} text={errors.name} />
            </div>
            <div className="flex min-w-0 flex-col gap-1.5">
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
                onChange={(event) => set("slots", event.target.value)}
                className="h-9 tabular-nums"
                aria-invalid={errors.slots ? true : undefined}
                aria-describedby={describedBy("slots")}
                data-testid="worker-settings-slots"
              />
              <p id={`${ids}-slots-hint`} className="text-xs text-muted-foreground">
                {t("slotsHint", { min: MIN_SLOTS, max: MAX_SLOTS })}
              </p>
              <FieldError id={`${ids}-slots-error`} text={errors.slots} />
            </div>
            <div className="flex min-w-0 flex-col gap-1.5">
              <Label htmlFor={`${ids}-labels`}>{t("labels")}</Label>
              <Input
                id={`${ids}-labels`}
                name="labels"
                value={form.labels}
                onChange={(event) => set("labels", event.target.value)}
                placeholder={t("labelsPlaceholder")}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={800}
                className="h-9 font-mono placeholder:font-sans"
                aria-invalid={errors.labels ? true : undefined}
                aria-describedby={describedBy("labels")}
                data-testid="worker-settings-labels"
              />
              <p id={`${ids}-labels-hint`} className="text-xs text-muted-foreground">
                {t("labelsHint")}
              </p>
              <FieldError id={`${ids}-labels-error`} text={errors.labels} />
            </div>
            <div className="flex min-w-0 flex-col gap-1.5 md:col-span-2">
              <Label htmlFor={`${ids}-plugins`}>{t("plugins")}</Label>
              <Input
                id={`${ids}-plugins`}
                name="plugins"
                value={form.plugins}
                onChange={(event) => set("plugins", event.target.value)}
                placeholder={t("pluginsPlaceholder")}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={1100}
                className="h-9 font-mono placeholder:font-sans"
                aria-invalid={errors.plugins ? true : undefined}
                aria-describedby={describedBy("plugins")}
                data-testid="worker-settings-plugins"
              />
              <p id={`${ids}-plugins-hint`} className="text-xs text-muted-foreground">
                {t("pluginsHint")}
              </p>
              <FieldError id={`${ids}-plugins-error`} text={errors.plugins} />
            </div>
            <fieldset className="flex min-w-0 flex-col gap-2 md:col-span-3" aria-describedby={describedBy("projects")}>
              <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3" data-testid="worker-settings-projects">
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
                            "projects",
                            event.target.checked
                              ? [...form.projects, name].sort()
                              : form.projects.filter((project) => project !== name),
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
              <p id={`${ids}-projects-hint`} className="text-xs text-muted-foreground">
                {t("projectsHint")}
              </p>
              <FieldError id={`${ids}-projects-error`} text={errors.projects} />
            </fieldset>
          </fieldset>
          {error ? <FormError text={error.text} detail={error.detail} requestId={error.requestId} testId="worker-settings-error" /> : null}
          {revoked ? null : (
            <div className="flex flex-wrap justify-end gap-2">
              {dirty ? (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    if (pending) return;
                    setEdits(null);
                    setErrors({});
                    write.reset();
                  }}
                  aria-disabled={pending || undefined}
                  data-testid="worker-settings-discard"
                >
                  <Undo2 aria-hidden="true" />
                  {t("discard")}
                </Button>
              ) : null}
              <Button type="submit" busy={pending} disabled={!dirty} data-testid="worker-settings-save">
                <Save aria-hidden="true" />
                {pending ? t("saving") : t("save")}
              </Button>
            </div>
          )}
        </form>
        {revoked ? null : <VersionLockSwitch worker={worker} />}
      </CardContent>
    </Card>
  );
}

function FieldError({ id, text }: { id: string; text?: string }) {
  if (!text) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger">
      {text}
    </p>
  );
}
