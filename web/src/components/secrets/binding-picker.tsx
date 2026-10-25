"use client";

import type { ComponentType, ReactNode } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import { Hint } from "./secret-form-parts";

/**
 * Where a secret goes, by name: the visitor's workers, or the visitor's pools. The hub takes one list or the other,
 * so while the other list holds a name, the names this one does not hold are locked. `choice` draws one checkbox: the
 * dialog passes the run form's, which this folder may not import.
 */
type ChoiceProps = {
  type: "checkbox";
  name: string;
  value: string;
  checked: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
  title: ReactNode;
  testId: string;
};

export function BindingPicker({
  id,
  kind,
  legend,
  hint,
  empty,
  loading,
  hidden,
  choices,
  chosen,
  locked,
  onToggle,
  choice: Choice,
}: {
  id: string;
  kind: "worker" | "pool";
  legend: string;
  hint: string;
  empty: string;
  loading: boolean;
  hidden: boolean;
  choices: string[];
  chosen: string[];
  locked: boolean;
  onToggle: (name: string, on: boolean) => void;
  choice: ComponentType<ChoiceProps>;
}) {
  return (
    <fieldset className={cn("flex min-w-0 flex-col gap-2", hidden && "hidden")} aria-describedby={`${id}-hint`} data-testid={`secret-${kind}s`}>
      <legend className="mb-1.5 text-sm font-medium">{legend}</legend>
      {loading ? (
        <Skeleton className="h-10 w-full sm:w-1/2" aria-hidden="true" />
      ) : choices.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid={`secret-no-${kind}s`}>
          {empty}
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {choices.map((name) => (
            <Choice
              key={name}
              type="checkbox"
              name={`${kind}s`}
              value={name}
              checked={chosen.includes(name)}
              disabled={locked && !chosen.includes(name)}
              onChange={(on) => onToggle(name, on)}
              title={<span className="font-mono">{name}</span>}
              testId={`secret-${kind}-${name}`}
            />
          ))}
        </div>
      )}
      <Hint id={`${id}-hint`}>{hint}</Hint>
    </fieldset>
  );
}
