"use client";

import type { LucideIcon } from "lucide-react";
import { useId } from "react";

import { cn } from "@/lib/utils";

export type FacetOption = {
  /** null is "all". */
  value: string | null;
  label: string;
  icon?: LucideIcon;
  /** Shown after the label; left out when it is not known (for example over search results). */
  count?: number;
  mono?: boolean;
};

type FacetGroupProps = {
  label: string;
  options: FacetOption[];
  selected: string | null;
  onSelect: (value: string | null) => void;
  /** The accessible text of a count, such as "12 memories". */
  countLabel: (count: number) => string;
  testId?: string;
};

/**
 * One facet as a row of toggle buttons in a labelled group: the pressed one is the filter in force, with its state
 * told by aria-pressed, a check of weight and border, not colour alone. Counts are tabular numbers.
 */
export function FacetGroup({ label, options, selected, onSelect, countLabel, testId }: FacetGroupProps) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-3" data-testid={testId}>
      <span id={id} className="shrink-0 pt-1.5 text-xs font-medium text-muted-foreground sm:w-20">
        {label}
      </span>
      <div role="group" aria-labelledby={id} className="flex flex-wrap gap-1.5">
        {options.map((option) => {
          const pressed = option.value === selected;
          const Icon = option.icon;
          return (
            <button
              key={option.value ?? "*all*"}
              type="button"
              aria-pressed={pressed}
              data-facet-value={option.value ?? ""}
              onClick={() => onSelect(pressed && option.value !== null ? null : option.value)}
              className={cn(
                "inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border px-3 text-xs transition-colors",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
                pressed
                  ? "border-brand bg-surface-selected font-semibold text-foreground"
                  : "border-border bg-card text-foreground hover:bg-muted",
              )}
            >
              {Icon ? <Icon className="size-3.5 shrink-0" aria-hidden="true" /> : null}
              <span className={cn(option.mono && "font-mono")}>{option.label}</span>{" "}
              {option.count !== undefined ? (
                <span
                  className={cn(
                    "rounded-full px-1.5 tabular-nums",
                    pressed ? "bg-background/60 text-accent-foreground" : "bg-muted text-muted-foreground",
                  )}
                >
                  <span aria-hidden="true">{option.count}</span>
                  <span className="sr-only">{countLabel(option.count)}</span>
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}
