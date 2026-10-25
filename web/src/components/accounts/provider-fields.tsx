"use client";

import { CircleCheck, CircleX, Download, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type RefObject, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";

import { EmptyPick, FieldError, Hint } from "./login-fields";
import { MAX_SECRET_NAME_CHARS, type SecretSummary } from "./login-queries";
import { addModel, type KeySource, modelChoices } from "./provider-model";
import { MAX_MODELS, type ProviderProbe } from "./provider-queries";

/** The parts of the provider form: where its key comes from, the result of a probe, and the models it serves. */

/** The props every key field has: the field's id, the key's input and the problems the form found in them. */
type KeyProps = {
  ids: string;
  source: KeySource;
  secret: string;
  secrets: readonly SecretSummary[];
  keyRef: RefObject<HTMLInputElement | null>;
  keyHint: string;
  keyProblem: string | null;
  secretProblem: string | null;
  onSource: (source: KeySource) => void;
  onSecret: (secret: string) => void;
  onKeyInput: () => void;
};

/**
 * The key: pasted in a password field the page never fills and that has no name, read when the form is sent and
 * emptied then; or an env secret of the visitor's, whose value the hub copies and the page never sees.
 */
export function KeyField({ ids, source, secret, secrets, keyRef, keyHint, keyProblem, secretProblem, onSource, onSecret, onKeyInput }: KeyProps) {
  const t = useTranslations("accounts.provider.form");
  const envSecrets = secrets.filter((item) => item.kind === "env");
  const describedBy = (field: string, problem: string | null) => [`${ids}-${field}-hint`, problem ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ");
  return (
    <fieldset className="flex min-w-0 flex-col gap-3 rounded-md border px-3 py-3" data-testid="provider-key-source">
      <legend className="px-1 text-sm font-medium">{t("key")}</legend>
      <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label={t("keySource")}>
        {(["paste", "secret"] as const).map((value) => (
          <label
            key={value}
            htmlFor={`${ids}-source-${value}`}
            className={cn(
              "flex min-h-10 cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 text-sm transition-colors hover:bg-accent max-md:min-h-11",
              source === value && "border-brand/40 bg-surface-selected hover:bg-surface-selected",
            )}
          >
            <input
              id={`${ids}-source-${value}`}
              type="radio"
              name={`${ids}-source`}
              value={value}
              checked={source === value}
              onChange={() => onSource(value)}
              className="size-4 shrink-0 cursor-pointer accent-primary"
              data-testid={`provider-source-${value}`}
            />
            {t(value === "paste" ? "sourcePaste" : "sourceSecret")}
          </label>
        ))}
      </div>
      {source === "paste" ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-key`}>{t("keyValue")}</Label>
          {/* No name attribute: the key is never part of a form submission the browser could make by itself. */}
          <Input
            ref={keyRef}
            id={`${ids}-key`}
            type="password"
            defaultValue=""
            onChange={onKeyInput}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className="h-9 font-mono"
            aria-invalid={keyProblem ? true : undefined}
            aria-describedby={describedBy("key", keyProblem)}
            aria-required="true"
            data-1p-ignore="true"
            data-lpignore="true"
            data-bwignore="true"
            data-form-type="other"
            data-testid="provider-key"
          />
          <Hint id={`${ids}-key-hint`}>{keyHint ? t("keyHintOf", { hint: keyHint }) : t("keyHint")}</Hint>
          <FieldError id={`${ids}-key-error`} text={keyProblem} testId="provider-key-error" />
        </div>
      ) : envSecrets.length === 0 ? (
        <EmptyPick testId="provider-no-secrets">{t("noSecrets")}</EmptyPick>
      ) : (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-secret`}>{t("secret")}</Label>
          <NativeSelect
            id={`${ids}-secret`}
            value={secret}
            onChange={(event) => onSecret(event.target.value)}
            className="w-full font-mono"
            aria-invalid={secretProblem ? true : undefined}
            aria-describedby={describedBy("secret", secretProblem)}
            data-testid="provider-from-secret"
          >
            <NativeSelectOption value="">{t("secretPick")}</NativeSelectOption>
            {envSecrets.map((item) => (
              <NativeSelectOption key={item.name} value={item.name}>
                {item.env_var ? `${item.name} (${item.env_var})` : item.name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
          <Hint id={`${ids}-secret-hint`}>{t("secretHint", { max: MAX_SECRET_NAME_CHARS })}</Hint>
          <FieldError id={`${ids}-secret-error`} text={secretProblem} testId="provider-from-secret-error" />
        </div>
      )}
    </fieldset>
  );
}

/** What Test connection or Fetch models found: the endpoint answered, with how many models, or why it did not. */
export function ProbeResult({ probe, failure }: { probe: ProviderProbe | null; failure: string | null }) {
  const t = useTranslations("accounts.provider.form");
  const ok = probe?.ok === true;
  const text = probe
    ? ok
      ? t("probeOk", { count: probe.count })
      : t("probeFailed", { status: probe.status ?? 0, error: probe.error ?? "" })
    : (failure ?? "");
  return (
    <p
      role="status"
      className={cn("flex items-start gap-2 text-sm", ok ? "text-success" : "text-danger")}
      data-testid="provider-test-result"
      data-ok={ok ? "true" : "false"}
    >
      {ok ? <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" /> : <CircleX className="mt-0.5 size-4 shrink-0" aria-hidden="true" />}
      <span className="min-w-0 [overflow-wrap:anywhere]">{text}</span>
    </p>
  );
}

/**
 * The models the account serves: those picked, those Fetch models listed and the catalog's, as checkboxes under a
 * search, and a field to add one by its id. The gateway names each `<prefix>/<model>` for the runs that ask for it.
 */
export function ModelsField({
  ids,
  picked,
  fetched,
  fallback,
  problem,
  fetching,
  canFetch,
  onFetch,
  onChange,
}: {
  ids: string;
  picked: readonly string[];
  fetched: readonly string[];
  fallback: string;
  problem: string | null;
  fetching: boolean;
  canFetch: boolean;
  onFetch: () => void;
  onChange: (models: string[]) => void;
}) {
  const t = useTranslations("accounts.provider.form");
  const [query, setQuery] = useState("");
  const [typed, setTyped] = useState("");
  const [addProblem, setAddProblem] = useState<string | null>(null);
  const choices = modelChoices(picked, fetched, fallback);
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = choices.filter((name) => words.every((word) => name.toLowerCase().includes(word)));
  const toggle = (name: string, on: boolean) => onChange(on ? [...picked, name] : picked.filter((item) => item !== name));
  const add = () => {
    const added = addModel(picked, typed);
    if (added.problem) {
      setAddProblem(t(`problems.${added.problem}`, { max: MAX_MODELS }));
      return;
    }
    onChange(added.models);
    setTyped("");
    setAddProblem(null);
  };
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={`${ids}-models-hint`} data-testid="provider-models">
      <legend className="mb-1.5 text-sm font-medium">{t("models")}</legend>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("modelsSearch")}
          aria-label={t("modelsSearch")}
          className="h-9 min-w-0 flex-1"
          data-testid="provider-models-search"
        />
        <Button type="button" variant="outline" onClick={onFetch} disabled={!canFetch} busy={fetching} data-testid="provider-fetch-models">
          <Download aria-hidden="true" />
          {t("fetchModels")}
        </Button>
      </div>
      <p className="text-xs text-fg-muted" aria-live="polite" data-testid="provider-models-picked">
        {t("modelsPicked", { count: picked.length, total: choices.length })}
      </p>
      {shown.length === 0 ? (
        <EmptyPick testId="provider-models-none">{choices.length === 0 ? t("modelsNone") : t("modelsNoMatch")}</EmptyPick>
      ) : (
        <ul className="grid max-h-56 gap-1.5 overflow-y-auto rounded-md border p-2 sm:grid-cols-2" data-testid="provider-models-list">
          {shown.map((name, index) => (
            <li key={name} className="min-w-0">
              <label htmlFor={`${ids}-model-${index}`} className="flex min-h-9 cursor-pointer items-center gap-2 rounded-sm px-2 py-1 hover:bg-accent max-md:min-h-11">
                <input
                  id={`${ids}-model-${index}`}
                  type="checkbox"
                  checked={picked.includes(name)}
                  onChange={(event) => toggle(name, event.target.checked)}
                  className="size-4 shrink-0 cursor-pointer accent-primary"
                />
                <span className="min-w-0 font-mono text-[13px] [overflow-wrap:anywhere]">{name}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-model-add`}>{t("modelAdd")}</Label>
        <div className="flex gap-2">
          <Input
            id={`${ids}-model-add`}
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
              setAddProblem(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                add();
              }
            }}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            placeholder={t("modelAddPlaceholder")}
            className="h-9 min-w-0 flex-1 font-mono placeholder:font-sans"
            aria-invalid={addProblem ? true : undefined}
            aria-describedby={addProblem ? `${ids}-model-add-error` : undefined}
            data-testid="provider-model-add"
          />
          <Button type="button" variant="outline" onClick={add} data-testid="provider-model-add-button">
            <Plus aria-hidden="true" />
            {t("modelAddButton")}
          </Button>
        </div>
        <FieldError id={`${ids}-model-add-error`} text={addProblem} testId="provider-model-add-error" />
      </div>
      <Hint id={`${ids}-models-hint`}>{t("modelsHint")}</Hint>
      <FieldError id={`${ids}-models-error`} text={problem} testId="provider-models-error" />
    </fieldset>
  );
}
