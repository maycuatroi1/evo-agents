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
  /** Show the label before the chips, where a toolbar holds more than one group; otherwise it only names the group. */
  showLabel?: boolean;
  options: FacetOption[];
  selected: string | null;
  onSelect: (value: string | null) => void;
  /** The accessible text of a count, such as "12 memories". */
  countLabel: (count: number) => string;
  testId?: string;
};

/**
 * One facet as the kit's filter chips: a labelled group of toggle buttons, 28 px with 6 px corners (44 px under
 * 768 px), the count after the label in tabular figures. The pressed chip is the filter in force: aria-pressed says
 * so, and it shows on `surface-selected` inside a `brand` edge in a heavier weight, so colour is not the only cue.
 */
export function FacetGroup({ label, showLabel = false, options, selected, onSelect, countLabel, testId }: FacetGroupProps) {
  const id = useId();
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5" data-testid={testId}>
      {showLabel ? (
        <span id={id} className="text-xs font-medium text-muted-foreground">
          {label}
        </span>
      ) : null}
      <div
        role="group"
        aria-labelledby={showLabel ? id : undefined}
        aria-label={showLabel ? undefined : label}
        className="flex min-w-0 flex-wrap gap-1.5"
      >
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
                "inline-flex h-7 max-w-full min-w-0 cursor-pointer items-center gap-1.5 rounded-sm border px-2.5 text-xs whitespace-nowrap transition-colors max-md:min-h-11",
                pressed
                  ? "border-brand bg-surface-selected font-semibold text-foreground"
                  : "border-border-strong bg-card font-medium text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              {Icon ? <Icon className="size-3.5 shrink-0" aria-hidden="true" /> : null}
              <span className={cn("truncate", option.mono && "font-mono")}>{option.label}</span>{" "}
              {option.count !== undefined ? (
                <span className="font-normal text-fg-subtle tabular-nums">
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
