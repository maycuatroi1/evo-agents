"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

import { type KeySpec, messageKey, type Problem } from "./policy-model";
import { useKeyLabel, useValueText } from "./policy-value";

/** The id of a key's field: `<base>-<scope>-<path>`, where scope is `policy` or a repo's index. */
export function fieldId(base: string, scope: string, path: string): string {
  return `${base}-${scope}-${path.replace(/[^a-z0-9_-]/gi, "-")}`;
}

/** What is wrong with a field, in the visitor's language. */
export function useProblemText() {
  const t = useTranslations("policy.problems");
  const format = (value: number) => String(value);
  return (problem: Problem) => {
    switch (problem.code) {
      case "oneOf":
        return t("oneOf", { options: problem.options.join(", ") });
      case "whole":
        return t("whole", { min: format(problem.min), max: format(problem.max) });
      case "number":
        return t("number", { min: format(problem.min), max: format(problem.max) });
      case "aboveNumber":
        return t("aboveNumber", { min: format(problem.min), max: format(problem.max) });
      case "line":
        return t("line", { max: problem.max });
      case "globs":
        return t("globs", { max: problem.max, chars: problem.chars });
      case "github":
        return t("github");
    }
  };
}

/** A group of the form: its title and one sentence of what its keys decide, over its fields. */
export function Group({ title, description, children, testId }: { title: string; description: string; children: ReactNode; testId: string }) {
  return (
    <fieldset className="min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <legend className="sr-only">{title}</legend>
      <div className="flex flex-col gap-0.5 border-b px-4 py-3" aria-hidden="true">
        <p className="text-[15px] leading-[22px] font-semibold">{title}</p>
        <p className="text-xs text-pretty text-muted-foreground">{description}</p>
      </div>
      <div className="grid gap-4 px-4 py-4 md:grid-cols-2 2xl:grid-cols-4">{children}</div>
    </fieldset>
  );
}

/**
 * One key of the policy as a field: a select for a choice, a number or text input, a text area of globs (one a line).
 * Its label, its hint (what it decides, and its default) and its problem are tied to it for screen readers.
 */
export function KeyField({
  base,
  scope,
  spec,
  value,
  onChange,
  problem,
  placeholder,
  disabledOptions = [],
  hint,
  className,
}: {
  base: string;
  scope: string;
  spec: KeySpec;
  value: string;
  onChange: (value: string) => void;
  problem?: Problem;
  placeholder?: string;
  disabledOptions?: readonly string[];
  hint?: ReactNode;
  className?: string;
}) {
  const t = useTranslations("policy");
  const label = useKeyLabel();
  const shown = useValueText();
  const text = useProblemText();
  const id = fieldId(base, scope, spec.path);
  const key = messageKey(spec.path);
  const described = [`${id}-hint`, problem ? `${id}-error` : ""].filter(Boolean).join(" ");
  const common = {
    id,
    "aria-invalid": problem ? true : undefined,
    "aria-describedby": described,
    "data-testid": `policy-field-${scope}-${spec.path}`,
  } as const;
  let control: ReactNode;
  if (spec.kind === "choice") {
    control = (
      <NativeSelect {...common} value={value} onChange={(event) => onChange(event.target.value)} className="w-full md:max-w-72">
        {(spec.options ?? []).map((option) => (
          <NativeSelectOption key={option} value={option} disabled={disabledOptions.includes(option)}>
            {shown(spec.path, option)}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    );
  } else if (spec.kind === "globs") {
    control = <Textarea {...common} rows={4} value={value} onChange={(event) => onChange(event.target.value)} className="font-mono" spellCheck={false} placeholder="**/billing/**" />;
  } else {
    const numeric = spec.kind === "whole" || spec.kind === "number";
    control = (
      <Input
        {...common}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        inputMode={spec.kind === "whole" ? "numeric" : spec.kind === "number" ? "decimal" : undefined}
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        className={cn(numeric ? "md:max-w-40 tabular-nums" : "font-mono placeholder:font-sans")}
      />
    );
  }
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", spec.kind === "globs" && "md:col-span-2", className)}>
      <Label htmlFor={id}>{label(spec.path)}</Label>
      {control}
      <p id={`${id}-hint`} className="text-xs text-pretty text-fg-subtle">
        {hint ?? t(`keys.${key}.hint` as Parameters<typeof t>[0])} {t("defaultIs", { value: shown(spec.path, spec.default) })}
      </p>
      {problem ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger" data-testid={`policy-error-${scope}-${spec.path}`}>
          {text(problem)}
        </p>
      ) : null}
    </div>
  );
}
