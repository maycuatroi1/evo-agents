"use client";

import { type ReactNode, useId } from "react";

import { InfoTip } from "@/components/feedback/info-tip";
import { cn } from "@/lib/utils";

/**
 * The parts the dispatch dialogs share (Dispatch, Run plan, and New plan and Revise with agent): fieldsets of bordered
 * choices whose details show from an info icon beside the legend.
 * `subject` says what is dispatched: runs of steps, one plan run, or one author run.
 */
export type DispatchSubject = "runs" | "plan" | "author";

/** A fieldset: its legend, with an info icon for `info`, a line below it for `hint`, then its choices. */
export function Section({
  legend,
  hint,
  info,
  children,
  testId,
  className,
}: {
  legend: string;
  hint?: ReactNode;
  info?: ReactNode;
  children: ReactNode;
  /** Of the fieldset; its info icon takes it with `-info`. */
  testId?: string;
  className?: string;
}) {
  const id = useId();
  return (
    <fieldset className={cn("flex min-w-0 flex-col gap-2", className)} aria-describedby={hint ? id : undefined} data-testid={testId}>
      <legend className="mb-1 flex items-center gap-1.5 text-sm font-medium">
        {legend}
        {info ? (
          <InfoTip label={legend} testId={testId ? `${testId}-info` : undefined}>
            {info}
          </InfoTip>
        ) : null}
      </legend>
      {hint ? (
        <p id={id} className="-mt-0.5 mb-0.5 text-xs text-pretty text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {children}
    </fieldset>
  );
}

/**
 * A radio as a compact tile: an icon, the name, and a short note below it for what is wrong with the choice (a runtime
 * none of the workers has). The tiles of a fieldset sit side by side as a segmented control: the radio itself is out of
 * sight but covers its tile, so a click anywhere on the tile lands on it, and the tile picked has a thicker brand
 * border; the fieldset's info icon says what each one does.
 */
export function Tile({
  name,
  value,
  checked,
  onChange,
  icon,
  title,
  note,
  disabled = false,
  testId,
}: {
  name: string;
  value: string;
  checked: boolean;
  onChange: () => void;
  icon?: ReactNode;
  title: ReactNode;
  note?: ReactNode;
  disabled?: boolean;
  testId?: string;
}) {
  const id = useId();
  return (
    <label
      htmlFor={id}
      className={cn(
        "relative flex min-h-11 min-w-0 items-center gap-2 rounded-md border px-3 py-2 transition-colors duration-150 has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50",
        disabled ? "cursor-not-allowed border-dashed bg-muted/40 text-muted-foreground" : "cursor-pointer hover:bg-muted/50",
        checked && !disabled && "border-brand bg-surface-selected shadow-[inset_0_0_0_1px_var(--color-brand)]",
      )}
      data-testid={testId}
      data-checked={checked || undefined}
    >
      <input
        id={id}
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={onChange}
        // Transparent over the whole tile rather than sr-only, which the tile's icon and words would cover.
        className={cn("absolute inset-0 m-0 size-full appearance-none opacity-0", disabled ? "cursor-not-allowed" : "cursor-pointer")}
        aria-describedby={note ? `${id}-note` : undefined}
      />
      {icon}
      <span className="flex min-w-0 flex-col">
        <span className="text-sm leading-5 whitespace-nowrap">{title}</span>
        {note ? (
          // Out of the radio's name, which the label around it gives; the radio reads it as its description.
          <span id={`${id}-note`} className="text-xs leading-4 text-attention" aria-hidden="true">
            {note}
          </span>
        ) : null}
      </span>
    </label>
  );
}

/** A radio or checkbox as a bordered card: the label, a line below it, and an optional badge. */
export function Choice({
  type,
  name,
  value,
  checked,
  disabled = false,
  onChange,
  title,
  hint,
  badge,
  testId,
}: {
  type: "radio" | "checkbox";
  name: string;
  value: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
  title: ReactNode;
  hint?: ReactNode;
  badge?: ReactNode;
  testId?: string;
}) {
  const id = useId();
  return (
    <label
      htmlFor={id}
      className={cn(
        "flex min-h-11 items-start gap-2.5 rounded-md border px-3 py-2 transition-colors",
        disabled ? "cursor-not-allowed border-dashed bg-muted/40" : "cursor-pointer hover:bg-muted/50",
        checked && !disabled && "border-brand/40 bg-surface-selected",
      )}
      data-testid={testId}
      data-disabled={disabled || undefined}
    >
      <input
        id={id}
        type={type}
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className={cn("mt-0.5 size-4 shrink-0 accent-primary", disabled ? "cursor-not-allowed" : "cursor-pointer")}
        aria-describedby={hint ? `${id}-hint` : undefined}
      />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-sm [overflow-wrap:anywhere]">{title}</span>
        {hint ? (
          <span id={`${id}-hint`} className="text-xs leading-snug text-muted-foreground [overflow-wrap:anywhere]">
            {hint}
          </span>
        ) : null}
      </span>
      {badge}
    </label>
  );
}
