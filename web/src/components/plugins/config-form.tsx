"use client";

import { LockKeyhole, RotateCcw, Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useMemo, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import type { PluginConfigLayer } from "@/lib/plugin-queries";

import {
  type ConfigField,
  type Draft,
  draftOf,
  type Problem,
  problemsOf,
  type SecretDraft,
  textOf,
  writeOf,
} from "./config-model";
import { type LayerTarget, useLayerWrite, usePluginFailure } from "./data";

type Props = {
  target: LayerTarget;
  fields: ConfigField[];
  layer: PluginConfigLayer;
  /** Whether the visitor may write this layer (a hub admin for the hub's, an admin of the project for its own). */
  canWrite: boolean;
  testId: string;
};

function useProblemText() {
  const t = useTranslations("plugins.config.problems");
  return (problem: Problem) => ("limit" in problem ? t(problem.kind, { limit: problem.limit }) : t(problem.kind));
}

/** A secret: whether the layer sets it, Replace and Clear, and a password field only while one is given. */
function SecretInput({
  field,
  held,
  draft,
  onChange,
  inputId,
  describedBy,
  invalid,
}: {
  field: ConfigField;
  held: boolean;
  draft: SecretDraft;
  onChange: (next: SecretDraft) => void;
  inputId: string;
  describedBy?: string;
  invalid: boolean;
}) {
  const t = useTranslations("plugins.config");
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Tag data-testid={`config-secret-${field.name}-state`}>
          <LockKeyhole aria-hidden="true" />
          {draft.mode === "clear" ? t("secretCleared") : held ? t("secretSet") : t("secretNotSet")}
        </Tag>
        {draft.mode === "keep" ? (
          <>
            <Button type="button" variant="outline" size="sm" onClick={() => onChange({ mode: "set", value: "" })} data-testid={`config-secret-${field.name}-replace`}>
              {held ? t("replace") : t("give")}
            </Button>
            {held ? (
              <Button type="button" variant="quiet-danger" size="sm" onClick={() => onChange({ mode: "clear", value: "" })}>
                {t("clear")}
              </Button>
            ) : null}
          </>
        ) : (
          <Button type="button" variant="ghost" size="sm" onClick={() => onChange({ mode: "keep", value: "" })}>
            {t("keep")}
          </Button>
        )}
      </div>
      {draft.mode === "set" ? (
        <Input
          id={inputId}
          type="password"
          value={draft.value}
          onChange={(event) => onChange({ mode: "set", value: event.target.value })}
          autoComplete="new-password"
          spellCheck={false}
          className="h-9 font-mono"
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          data-testid={`config-field-${field.name}`}
        />
      ) : null}
    </div>
  );
}

/** One plain field as the input its kind takes; empty means "not set here". */
function PlainInput({
  field,
  value,
  onChange,
  inputId,
  describedBy,
  invalid,
}: {
  field: ConfigField;
  value: string;
  onChange: (next: string) => void;
  inputId: string;
  describedBy?: string;
  invalid: boolean;
}) {
  const t = useTranslations("plugins.config");
  const common = {
    id: inputId,
    "aria-describedby": describedBy,
    "aria-invalid": invalid || undefined,
    "data-testid": `config-field-${field.name}`,
  };
  if (field.kind === "choice" || field.kind === "boolean") {
    return (
      <NativeSelect {...common} value={value} onChange={(event) => onChange(event.target.value)} className="w-full md:max-w-xs">
        <NativeSelectOption value="">{t("notSetHere")}</NativeSelectOption>
        {field.choices.map((choice) => {
          const key = JSON.stringify(choice);
          const label = choice === true ? t("on") : choice === false ? t("off") : typeof choice === "string" ? choice : key;
          return (
            <NativeSelectOption key={key} value={key}>
              {label}
            </NativeSelectOption>
          );
        })}
      </NativeSelect>
    );
  }
  if (field.kind === "list" || field.kind === "json") {
    return (
      <Textarea
        {...common}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={field.kind === "json" ? 4 : 3}
        spellCheck={false}
        className="font-mono text-[13px]"
        placeholder={field.fallback ?? (field.kind === "list" ? t("listPlaceholder") : "{}")}
      />
    );
  }
  const numeric = field.kind === "integer" || field.kind === "number";
  return (
    <Input
      {...common}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      inputMode={numeric ? (field.kind === "integer" ? "numeric" : "decimal") : undefined}
      autoComplete="off"
      spellCheck={false}
      className={numeric ? "h-9 md:max-w-xs" : "h-9"}
      placeholder={field.fallback === null ? undefined : t("defaultPlaceholder", { value: field.fallback })}
    />
  );
}

/**
 * The form of one stored layer of a plugin's config (web/DESIGN.md, Plugins): a field for each property of the
 * plugin's schema, by kind, its title and description, "Required" for what the effective config needs, and the
 * schema's default as a placeholder. Save sends only what changed and the version the layer is at; nothing goes out
 * while a field is wrong, and the first wrong field takes focus. A refusal of the hub shows above Save; the version
 * written is a toast. Someone who may not write reads what the layer sets instead.
 */
export function ConfigForm({ target, fields, layer, canWrite, testId }: Props) {
  const t = useTranslations("plugins.config");
  const problemText = useProblemText();
  const ids = useId();
  const write = useLayerWrite(target);
  const failure = usePluginFailure();
  const initial = useMemo(() => draftOf(fields, layer), [fields, layer]);
  const [draft, setDraft] = useState<Draft>(initial);
  const [problems, setProblems] = useState<Record<string, Problem>>({});
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const dirty = JSON.stringify(draft) !== JSON.stringify(initial);

  if (!canWrite) {
    const set = fields.filter((field) => (field.kind === "secret" ? layer.secrets.includes(field.name) : field.name in layer.values));
    return (
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-[minmax(8rem,auto)_1fr]" data-testid={testId}>
        {set.length === 0 ? <p className="text-sm text-muted-foreground">{t("layerEmpty")}</p> : null}
        {set.map((field) => (
          <div key={field.name} className="contents">
            <dt className="text-[13px] text-muted-foreground">{field.title}</dt>
            <dd className="min-w-0 font-mono text-[13px] [overflow-wrap:anywhere]">
              {field.kind === "secret" ? t("secretSet") : textOf(field.kind, layer.values[field.name])}
            </dd>
          </div>
        ))}
      </dl>
    );
  }

  const change = (name: string, value: string | SecretDraft) => {
    setDraft((current) => ({ ...current, [name]: value }));
    setProblems((current) => Object.fromEntries(Object.entries(current).filter(([key]) => key !== name)));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (write.isPending) return;
    const found = problemsOf(fields, draft);
    setProblems(found);
    const first = fields.find((field) => found[field.name]);
    if (first) {
      document.getElementById(`${ids}-${first.name}`)?.focus();
      return;
    }
    const body = writeOf(fields, layer, draft);
    if (body === null) return;
    setRefused(null);
    try {
      const saved = await write.mutateAsync(body);
      notify({ tone: "success", text: t("saved", { version: saved.version }) });
    } catch (error) {
      setRefused(failure(error, { 409: t("conflict") }));
    } finally {
      // A secret given is sent once and not kept in the page, whatever the hub answered.
      setDraft((current) =>
        Object.fromEntries(
          Object.entries(current).map(([name, value]) => [name, typeof value === "string" ? value : { mode: "keep", value: "" }]),
        ),
      );
    }
  };

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5" aria-busy={write.isPending || undefined} data-testid={testId}>
      {fields.length === 0 ? <p className="text-sm text-muted-foreground">{t("noFields")}</p> : null}
      {fields.map((field) => {
        const inputId = `${ids}-${field.name}`;
        const problem = problems[field.name];
        const describedBy = [field.description ? `${inputId}-hint` : "", problem ? `${inputId}-error` : ""].filter(Boolean).join(" ") || undefined;
        const held = draft[field.name];
        return (
          <div key={field.name} className="flex flex-col gap-1.5" data-config-field={field.name}>
            <div className="flex flex-wrap items-center gap-2">
              <Label htmlFor={inputId}>{field.title}</Label>
              {field.required ? <Tag>{t("required")}</Tag> : null}
              {field.title !== field.name ? <code className="font-mono text-xs text-fg-subtle">{field.name}</code> : null}
            </div>
            {field.description ? (
              <p id={`${inputId}-hint`} className="text-xs text-fg-subtle">
                {field.description}
              </p>
            ) : null}
            {field.kind === "secret" && typeof held !== "string" ? (
              <SecretInput
                field={field}
                held={layer.secrets.includes(field.name)}
                draft={held}
                onChange={(next) => change(field.name, next)}
                inputId={inputId}
                describedBy={describedBy}
                invalid={Boolean(problem)}
              />
            ) : (
              <PlainInput
                field={field}
                value={typeof held === "string" ? held : ""}
                onChange={(next) => change(field.name, next)}
                inputId={inputId}
                describedBy={describedBy}
                invalid={Boolean(problem)}
              />
            )}
            {problem ? (
              <p id={`${inputId}-error`} className="text-xs text-danger" data-testid={`config-problem-${field.name}`}>
                {problemText(problem)}
              </p>
            ) : null}
          </div>
        );
      })}
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      {fields.length > 0 ? (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-4">
          {dirty ? (
            <Button type="button" variant="ghost" onClick={() => setDraft(initial)} disabled={write.isPending}>
              <RotateCcw aria-hidden="true" />
              {t("discard")}
            </Button>
          ) : null}
          <Button type="submit" busy={write.isPending} disabled={!dirty} data-testid={`${testId}-save`}>
            <Save aria-hidden="true" />
            {write.isPending ? t("saving") : t("save")}
          </Button>
        </div>
      ) : null}
    </form>
  );
}
