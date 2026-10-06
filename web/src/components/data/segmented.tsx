"use client";

import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

export type SegmentedOption<T extends string> = {
  value: T;
  label: string;
  icon?: LucideIcon;
  testId?: string;
};

/**
 * The kit's segmented control: two or three ways to show the same thing (board or list, unified or split, rendered
 * or raw). A labelled group of toggle buttons on `surface-sunken` with 6 px corners; the pressed one sits on `surface`
 * inside a `border-strong` ring, in `fg`, and says so with aria-pressed. 24 px buttons with 12 px text, 44 px under
 * 768 px like every other control.
 */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  className,
}: {
  label: string;
  value: T;
  options: readonly SegmentedOption<T>[];
  onChange: (value: T) => void;
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn("inline-flex w-fit gap-0.5 rounded-sm border bg-muted p-0.5", className)}
    >
      {options.map(({ value: id, label: text, icon: Icon, testId }) => (
        <button
          key={id}
          type="button"
          aria-pressed={value === id}
          onClick={() => onChange(id)}
          className={cn(
            "inline-flex h-6 cursor-pointer items-center gap-1.5 rounded-xs px-2.5 text-xs font-medium whitespace-nowrap transition-colors duration-fast ease-standard max-md:min-h-11",
            value === id
              ? "bg-card text-foreground ring-1 ring-border-strong"
              : "text-muted-foreground hover:text-foreground",
          )}
          data-testid={testId}
        >
          {Icon ? <Icon className="size-3.5 shrink-0" aria-hidden="true" /> : null}
          {text}
        </button>
      ))}
    </div>
  );
}
