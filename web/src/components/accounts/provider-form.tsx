"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Info, PlugZap, Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import { projectsQuery, whoamiQuery } from "@/lib/queries";

import { NumberField } from "./form-parts";
import { FailureBox } from "./hooks";
import { CheckList, CheckListSkeleton, EmptyPick, FieldError, Hint, Note } from "./login-fields";
import { utcDay } from "./login-model";
import { MAX_SECRET_BYTES, MAX_SECRET_NAME_CHARS, secretNamesQuery, workersQuery } from "./login-queries";
import { accountProblems, type Problem } from "./model";
import { KeyField, ModelsField, ProbeResult } from "./provider-fields";
import { useProbe, useProviderFailure, useProviderWrite } from "./provider-hooks";
import { ProviderMark } from "./provider-mark";
import {
  editProviderForm,
  newProviderForm,
  probeBody,
  PROVIDER_FIELDS,
  providerChangeBody,
  type ProviderField,
  type ProviderForm,
  type ProviderProblems,
  providerProblems,
  providerWriteBody,
  takenNames,
} from "./provider-model";
import { API_FORMATS, type CatalogProvider, MAX_MODELS, type ProviderAccount, providerAccountsQuery } from "./provider-queries";
import { MAX_PRIORITY, MAX_WEIGHT } from "./queries";

export type ProviderTarget = { kind: "add"; entry: CatalogProvider } | { kind: "edit"; entry: CatalogProvider | null; account: ProviderAccount };

/**
 * Add a provider account of a catalog entry, or Edit one: its name, base URL and API format (the catalog's, which the
 * person may change), its key (pasted, or an env secret of theirs), the projects and workers whose runs may take it,
 * the day it ends, and its models, with Test connection and Fetch models to ask the endpoint first. Edit keeps the
 * key and sends only what changed, with the weight and priority by which a pool of the provider picks it; Change key
 * is its own dialog. The key field is emptied as the form is sent, before the hub answers.
 */
export function ProviderFormBody({
  target,
  onBack,
  onBusy,
  onClose,
}: {
  target: ProviderTarget;
  onBack?: () => void;
  onBusy: (busy: boolean) => void;
  onClose: () => void;
}) {
  const t = useTranslations("accounts.provider.form");
  const tProblem = useTranslations("accounts.provider.form.problems");
  const tRank = useTranslations("accounts.form");
  const ids = useId();
  const editing = target.kind === "edit";
  const entry = target.entry;
  const providerId = target.kind === "edit" ? target.account.provider : target.entry.id;
  const write = useProviderWrite();
  const tester = useProbe();
  const fetcher = useProbe();
  const failureOf = useProviderFailure();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const projects = useQuery(projectsQuery(browserApi));
  const workers = useQuery(workersQuery(browserApi));
  const secrets = useQuery(secretNamesQuery(browserApi));
  const providers = useQuery(providerAccountsQuery(browserApi));
  const taken = useMemo(() => takenNames(secrets.data ?? [], providers.data ?? []), [secrets.data, providers.data]);
  const [draft, setForm] = useState<ProviderForm>(() =>
    target.kind === "edit" ? editProviderForm(target.account) : newProviderForm(target.entry, new Set()),
  );
  const [rank, setRank] = useState(() => (target.kind === "edit" ? { weight: String(target.account.weight), priority: String(target.account.priority) } : null));
  const [named, setNamed] = useState(false);
  const [errors, setErrors] = useState<ProviderProblems>({});
  const [sent, setSent] = useState(false);
  const [forgotten, setForgotten] = useState(false);
  const [fetched, setFetched] = useState<string[]>([]);
  const keyRef = useRef<HTMLInputElement>(null);
  const pending = write.pending;
  useEffect(() => onBusy(pending), [onBusy, pending]);

  // The first free name, once the visitor's names are known, until the person types one.
  const suggested = target.kind === "add" && !named ? newProviderForm(target.entry, taken).name : null;
  const form = suggested === null ? draft : { ...draft, name: suggested };

  const projectChoices = useMemo(
    () => [...new Set([...(projects.data ?? []).filter((p) => p.role === "writer" || p.role === "admin").map((p) => p.name), ...form.projects])].sort(),
    [projects.data, form.projects],
  );
  const workerChoices = useMemo(
    () =>
      [...new Set([...(workers.data ?? []).filter((w) => w.owner === me?.login && w.status !== "revoked").map((w) => w.name), ...form.workers])].sort((a, b) =>
        a.localeCompare(b),
      ),
    [workers.data, me?.login, form.workers],
  );
  const [minDay] = useState(() => utcDay(Date.now() + 24 * 60 * 60 * 1000));

  const set = (patch: Partial<ProviderForm>, field?: ProviderField) => {
    setForm((current) => ({ ...current, ...patch }));
    if (field) setErrors((current) => ({ ...current, [field]: undefined }));
  };
  const toggle = (list: "projects" | "workers", name: string, on: boolean) =>
    set({ [list]: on ? [...form[list], name] : form[list].filter((item) => item !== name) }, list === "projects" ? "projects" : undefined);
  const describedBy = (field: ProviderField) => [`${ids}-${field}-hint`, errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ");
  const problem = (field: ProviderField) => {
    const found = errors[field];
    if (!found) return null;
    return tProblem(found, { max: found === "modelsTooMany" ? MAX_MODELS : MAX_SECRET_BYTES, name: form.name });
  };
  const rankProblems = sent && rank ? accountProblems(rank) : {};
  const sayRank = (found: Problem | undefined, max: number) => (found ? tRank(`problems.${found}`, { min: 0, max }) : null);

  const probe = (model: string | null) =>
    target.kind === "edit"
      ? { name: target.account.name, model }
      : { body: probeBody(form, providerId, keyRef.current?.value ?? "", model) };
  const test = () => void tester.probe(probe(form.models[0] ?? (entry?.default_model || null)));
  const fetchModels = async () => {
    const { probe: found } = await fetcher.probe(probe(null));
    if (found?.ok) setFetched(found.models);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setSent(true);
    const field = keyRef.current;
    const found = providerProblems(form, { key: field?.value ?? "", taken, editing, today: utcDay(Date.now()) });
    setErrors(found);
    const first = PROVIDER_FIELDS.find((key) => found[key]);
    if (first) {
      document.getElementById(first === "projects" ? `${ids}-project-0` : `${ids}-${first}`)?.focus();
      return;
    }
    if (rank && Object.keys(accountProblems(rank)).length > 0) {
      document.getElementById(`${ids}-weight`)?.focus();
      return;
    }
    if (target.kind === "edit") {
      const change = providerChangeBody(form, target.account) ?? {};
      if (rank && Number(rank.weight) !== target.account.weight) change.weight = Number(rank.weight);
      if (rank && Number(rank.priority) !== target.account.priority) change.priority = Number(rank.priority);
      if (Object.keys(change).length === 0) return onClose();
      const done = await write.change(target.account.name, change);
      if (!done.ok) return;
      notify({ tone: "success", text: t("saved", { name: target.account.name }), description: t("savedText") });
      return onClose();
    }
    const body = providerWriteBody(form, providerId, field?.value ?? "");
    // The key leaves the page with this request and is not kept: the field is emptied before the hub answers.
    if (field) field.value = "";
    setForgotten(false);
    const done = await write.put(form.name.trim(), body);
    if (!done.ok) {
      setForgotten(form.source === "paste");
      return;
    }
    notify({ tone: "success", text: t("added", { name: form.name.trim() }), description: t("addedText", { alias: done.value.alias }) });
    onClose();
  };

  const failure = write.error ? failureOf(write.error) : null;
  const probeFailure = (result: typeof tester.result) => (result?.error ? failureOf(result.error).text : null);
  const name = entry?.name ?? (target.kind === "edit" ? target.account.provider_name : providerId);

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        {onBack ? (
          <Button type="button" variant="ghost" size="icon" className="-ml-1 shrink-0" onClick={onBack} disabled={pending} aria-label={t("back")} data-testid="provider-back">
            <ArrowLeft aria-hidden="true" />
          </Button>
        ) : null}
        <ProviderMark id={providerId} entry={entry} size="sm" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <DialogTitle className="text-lg font-semibold [overflow-wrap:anywhere]">
            {editing ? t("editTitle", { name: target.account.name }) : t("addTitle", { provider: name })}
          </DialogTitle>
          <DialogDescription className="text-pretty">{editing ? t("editDescription") : t("addDescription")}</DialogDescription>
        </div>
      </DialogHeader>

      <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5" aria-busy={pending || undefined} data-testid="provider-form" data-mode={target.kind}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
          <Input
            id={`${ids}-name`}
            value={form.name}
            onChange={(event) => {
              setNamed(true);
              set({ name: event.target.value }, "name");
            }}
            readOnly={editing}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={MAX_SECRET_NAME_CHARS}
            className="h-9 font-mono read-only:bg-muted read-only:text-muted-foreground"
            aria-invalid={errors.name ? true : undefined}
            aria-describedby={describedBy("name")}
            aria-required={editing ? undefined : "true"}
            data-testid="provider-name"
          />
          <Hint id={`${ids}-name-hint`}>{editing ? t("nameLocked") : t("nameHint")}</Hint>
          <FieldError id={`${ids}-name-error`} text={problem("name")} testId="provider-name-error" />
        </div>

        <div className="grid gap-4 sm:grid-cols-[1fr_12rem]">
          <div className="flex min-w-0 flex-col gap-1.5">
            <Label htmlFor={`${ids}-baseUrl`}>{t("baseUrl")}</Label>
            <Input
              id={`${ids}-baseUrl`}
              type="url"
              inputMode="url"
              value={form.baseUrl}
              onChange={(event) => set({ baseUrl: event.target.value }, "baseUrl")}
              placeholder="https://api.example.com/v1"
              autoComplete="off"
              spellCheck={false}
              className="h-9 font-mono"
              aria-invalid={errors.baseUrl ? true : undefined}
              aria-describedby={describedBy("baseUrl")}
              aria-required="true"
              data-testid="provider-base-url"
            />
            <Hint id={`${ids}-baseUrl-hint`}>{t("baseUrlHint")}</Hint>
            <FieldError id={`${ids}-baseUrl-error`} text={problem("baseUrl")} testId="provider-base-url-error" />
          </div>
          <div className="flex min-w-0 flex-col gap-1.5">
            <Label htmlFor={`${ids}-format`}>{t("format")}</Label>
            <NativeSelect id={`${ids}-format`} value={form.format} onChange={(event) => set({ format: event.target.value as ProviderForm["format"] })} className="w-full" data-testid="provider-format">
              {API_FORMATS.map((format) => (
                <NativeSelectOption key={format} value={format}>
                  {t(`formats.${format}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </div>
        </div>

        {editing ? null : (
          <KeyField
            ids={ids}
            source={form.source}
            secret={form.secret}
            secrets={secrets.data ?? []}
            keyRef={keyRef}
            keyHint={entry?.key_hint ?? ""}
            keyProblem={problem("key")}
            secretProblem={problem("secret")}
            onSource={(source) => set({ source }, form.source === "paste" ? "key" : "secret")}
            onSecret={(secret) => set({ secret }, "secret")}
            onKeyInput={() => {
              setErrors((current) => ({ ...current, key: undefined }));
              setForgotten(false);
            }}
          />
        )}

        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" onClick={test} busy={tester.pending} disabled={!form.baseUrl.trim()} data-testid="provider-test">
              <PlugZap aria-hidden="true" />
              {t("test")}
            </Button>
            <span className="text-xs text-fg-muted">{editing ? t("testSaved") : t("testHint")}</span>
          </div>
          {tester.result ? <ProbeResult probe={tester.result.probe} failure={probeFailure(tester.result)} /> : null}
        </div>

        <ModelsField
          ids={ids}
          picked={form.models}
          fetched={fetched}
          fallback={entry?.default_model ?? ""}
          problem={problem("models")}
          fetching={fetcher.pending}
          canFetch={Boolean(form.baseUrl.trim())}
          onFetch={() => void fetchModels()}
          onChange={(models) => set({ models }, "models")}
        />
        {fetcher.result && !fetcher.result.probe?.ok ? <ProbeResult probe={fetcher.result.probe} failure={probeFailure(fetcher.result)} /> : null}

        <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={describedBy("projects")}>
          <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
          {projects.isPending ? (
            <CheckListSkeleton />
          ) : projectChoices.length === 0 ? (
            <EmptyPick testId="provider-no-projects">{t("noProjects")}</EmptyPick>
          ) : (
            <CheckList idPrefix={`${ids}-project`} names={projectChoices} picked={form.projects} onToggle={(item, on) => toggle("projects", item, on)} invalid={Boolean(errors.projects)} testId="provider-projects" />
          )}
          <Hint id={`${ids}-projects-hint`}>{t("projectsHint")}</Hint>
          <FieldError id={`${ids}-projects-error`} text={problem("projects")} testId="provider-projects-error" />
        </fieldset>

        <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={`${ids}-workers-hint`}>
          <legend className="mb-1.5 text-sm font-medium">{t("workers")}</legend>
          {workers.isPending ? (
            <CheckListSkeleton />
          ) : workerChoices.length === 0 ? (
            <EmptyPick testId="provider-no-workers">{t("noWorkers")}</EmptyPick>
          ) : (
            <CheckList idPrefix={`${ids}-worker`} names={workerChoices} picked={form.workers} onToggle={(item, on) => toggle("workers", item, on)} testId="provider-workers" />
          )}
          <Hint id={`${ids}-workers-hint`}>{t("workersHint")}</Hint>
        </fieldset>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-expires`}>{t("expires")}</Label>
          <Input
            id={`${ids}-expires`}
            type="date"
            min={minDay}
            value={form.expires}
            onChange={(event) => set({ expires: event.target.value }, "expires")}
            className="h-9 w-full tabular-nums sm:w-48"
            aria-invalid={errors.expires ? true : undefined}
            aria-describedby={describedBy("expires")}
            data-testid="provider-expires"
          />
          <Hint id={`${ids}-expires-hint`}>{t("expiresHint")}</Hint>
          <FieldError id={`${ids}-expires-error`} text={problem("expires")} testId="provider-expires-error" />
        </div>

        {rank ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <NumberField id={`${ids}-weight`} label={tRank("weight")} hint={tRank("weightHint", { max: MAX_WEIGHT })} problem={sayRank(rankProblems.weight, MAX_WEIGHT)} value={rank.weight} onChange={(weight) => setRank({ ...rank, weight })} testId="provider-weight" />
            <NumberField id={`${ids}-priority`} label={tRank("priority")} hint={tRank("priorityHint", { max: MAX_PRIORITY })} problem={sayRank(rankProblems.priority, MAX_PRIORITY)} value={rank.priority} onChange={(priority) => setRank({ ...rank, priority })} testId="provider-priority" />
          </div>
        ) : null}

        {failure ? (
          <div className="flex flex-col gap-2">
            <FailureBox failure={failure} requestLabel={t("requestId", { id: failure.requestId ?? "" })} />
            {forgotten ? <Note tone="info" icon={Info} text={t("keyForgotten")} testId="provider-key-forgotten" /> : null}
          </div>
        ) : null}
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline" size="lg" aria-disabled={pending || undefined} className="aria-disabled:opacity-50" onClick={(event) => pending && event.preventDefault()}>
              {t("cancel")}
            </Button>
          </DialogClose>
          <Button type="submit" size="lg" busy={pending} data-testid="provider-save">
            <Save aria-hidden="true" />
            {pending ? t("saving") : editing ? t("save") : t("add")}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
