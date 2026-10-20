"use client";

import { TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOptGroup, NativeSelectOption } from "@/components/ui/native-select";

import { FieldLabel } from "./field-label";
import type { ModelProblem, RuntimeName } from "./model";
import { MAX_MODEL_CHARS, type RequestedRuntime, RUNTIMES } from "./queries";

/** The dropdown's value of a model: its runtime and its name, apart, since an opencode model holds a slash itself. */
const SEPARATOR = "\u001f";
const OTHER = "=other";

/**
 * The model a run uses, as a dropdown of the models the visitor's workers list (`models`, by runtime): the runtime's
 * default first, then those of the runtime picked, or of every runtime for any, where picking one picks its runtime
 * too (`onRuntime`); last, another model, typed as its runtime names it. The API takes a model with any runtime; this
 * field asks for a runtime with a typed model, since a model name means something only to the runtime it is for.
 */
export function ModelField({
  value,
  onChange,
  runtime,
  models,
  onRuntime,
  problem,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  runtime: RequestedRuntime;
  models: Partial<Record<RuntimeName, string[]>>;
  onRuntime?: (runtime: RuntimeName) => void;
  problem: ModelProblem | null;
  disabled?: boolean;
}) {
  const t = useTranslations("runs.planRun.model");
  const tRuntime = useTranslations("runs.runtime");
  const ids = useId();
  const [other, setOther] = useState(false);
  const groups = (runtime === "any" ? RUNTIMES : [runtime]).flatMap((name) => {
    const listed = models[name] ?? [];
    return listed.length ? [{ runtime: name, models: listed }] : [];
  });
  const model = value.trim();
  const listed = runtime !== "any" && (models[runtime] ?? []).includes(model);
  const typing = other || (model !== "" && !listed);
  const selected = typing ? OTHER : model === "" ? "" : `${runtime}${SEPARATOR}${model}`;
  const message = problem === null ? null : problem === "long" ? t("long", { max: MAX_MODEL_CHARS }) : t(problem);

  const pick = (next: string) => {
    if (next === OTHER) {
      setOther(true);
      return;
    }
    setOther(false);
    if (next === "") return onChange("");
    const [name, picked] = next.split(SEPARATOR) as [RuntimeName, string];
    if (name !== runtime) onRuntime?.(name);
    onChange(picked);
  };

  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="plan-run-model">
      <FieldLabel htmlFor={`${ids}-select`} label={t("label")} info={t("info")} testId="plan-run-model-info" />
      <NativeSelect
        id={`${ids}-select`}
        value={selected}
        onChange={(event) => pick(event.target.value)}
        disabled={disabled}
        className="w-full md:[&_select]:h-9"
        data-testid="plan-run-model-select"
      >
        <NativeSelectOption value="">{runtime === "any" ? t("default") : t("defaultOf", { runtime: tRuntime(runtime) })}</NativeSelectOption>
        {groups.map((group) => (
          <NativeSelectOptGroup key={group.runtime} label={tRuntime(group.runtime)}>
            {group.models.map((name) => (
              <NativeSelectOption key={name} value={`${group.runtime}${SEPARATOR}${name}`}>
                {name}
              </NativeSelectOption>
            ))}
          </NativeSelectOptGroup>
        ))}
        <NativeSelectOption value={OTHER}>{t("other")}</NativeSelectOption>
      </NativeSelect>
      {typing ? (
        <Input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={runtime === "opencode" ? t("placeholderOpencode") : t("placeholder")}
          aria-label={t("typed")}
          autoComplete="off"
          spellCheck={false}
          autoFocus={other && model === ""}
          disabled={disabled}
          aria-invalid={problem !== null || undefined}
          aria-describedby={message ? `${ids}-error` : undefined}
          className="h-9 font-mono placeholder:font-sans"
          data-testid="plan-run-model-input"
        />
      ) : null}
      {message ? (
        <p id={`${ids}-error`} className="flex items-start gap-1.5 text-xs font-medium text-danger" data-testid="plan-run-model-error">
          <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          {message}
        </p>
      ) : null}
    </div>
  );
}
