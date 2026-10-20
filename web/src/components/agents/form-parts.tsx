"use client";

import { Check, ImageUp, Palette, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { PALETTE } from "@/lib/agent-rules";
import { AVATAR_MAX_BYTES, AVATAR_TYPES, avatarProblem, inkOn } from "@/lib/avatars";
import { cn } from "@/lib/utils";

/** A field of the agent form: its label, the control (given the ids to point at), a hint, and its error. */
export function Field({
  label,
  optional,
  hint,
  error,
  testId,
  children,
}: {
  label: string;
  optional?: string;
  hint?: ReactNode;
  error?: string | null;
  testId?: string;
  children: (ids: { id: string; describedBy: string; invalid: boolean }) => ReactNode;
}) {
  const id = useId();
  const describedBy = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(" ");
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid={testId}>
      <label htmlFor={id} className="text-sm font-medium">
        {label} {optional ? <span className="font-normal text-muted-foreground">{optional}</span> : null}
      </label>
      {children({ id, describedBy, invalid: Boolean(error) })}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? <FieldError id={`${id}-error`} text={error} /> : null}
    </div>
  );
}

export function FieldError({ id, text, testId = "agent-field-error" }: { id?: string; text: string; testId?: string }) {
  return (
    <p id={id} className="flex items-start gap-1.5 text-xs font-medium text-pretty text-danger" data-testid={testId}>
      <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      {text}
    </p>
  );
}

/** The palette's colours in words, in its order, for the swatches' names. */
const PALETTE_NAMES = ["blue", "violet", "pink", "red", "amber", "green", "cyan", "indigo"] as const;

const SWATCH = "relative grid size-8 cursor-pointer place-items-center rounded-full max-md:size-11";
const SWATCH_RING =
  "pointer-events-none absolute -inset-1 rounded-full border-2 border-transparent peer-checked:border-foreground peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-ring";

/**
 * The agent's colour, behind its initial wherever it has no avatar: one of the hub's palette, a colour of the admin's
 * own, or (creating only, `value` null) the one the hub picks by the handle. The chosen swatch carries a check and a
 * ring, so the choice is not told by colour alone; each swatch is named for screen readers.
 */
export function ColourField({ value, onChange, allowAuto, disabled }: { value: string | null; onChange: (value: string | null) => void; allowAuto: boolean; disabled?: boolean }) {
  const t = useTranslations("agents.form.colour");
  const name = useId();
  const custom = value !== null && !(PALETTE as readonly string[]).includes(value);
  const swatch = (colour: string | null, label: string, testId: string) => (
    <label className={SWATCH} title={label} data-testid={testId}>
      <input
        type="radio"
        name={name}
        className="peer sr-only"
        checked={value === colour}
        onChange={() => onChange(colour)}
        disabled={disabled}
        aria-label={label}
      />
      <span className={SWATCH_RING} aria-hidden="true" />
      {colour ? (
        <span className="grid size-full place-items-center rounded-full" style={{ backgroundColor: colour, color: inkOn(colour) }} aria-hidden="true">
          {value === colour ? <Check className="size-4" /> : null}
        </span>
      ) : (
        <span className="grid size-full place-items-center rounded-full border border-dashed border-input text-[11px] font-medium text-muted-foreground" aria-hidden="true">
          {t("autoShort")}
        </span>
      )}
    </label>
  );
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" data-testid="agent-colour">
      <legend className="mb-1 text-sm font-medium">{t("label")}</legend>
      <div className="flex flex-wrap items-center gap-3">
        {allowAuto ? swatch(null, t("auto"), "agent-colour-auto") : null}
        {PALETTE.map((colour, index) => swatch(colour, t(`palette.${PALETTE_NAMES[index]}`), `agent-colour-${colour.slice(1)}`))}
        <label className="inline-flex min-h-8 cursor-pointer items-center gap-2 rounded-sm px-1 text-[13px] text-muted-foreground max-md:min-h-11">
          <Palette className="size-4" aria-hidden="true" />
          <span>{t("custom")}</span>
          <input
            type="color"
            value={value ?? PALETTE[0]}
            onChange={(event) => onChange(event.target.value.toLowerCase())}
            disabled={disabled}
            className={cn("size-8 cursor-pointer rounded-sm border border-input bg-card p-0.5 max-md:size-11", custom && "ring-2 ring-foreground ring-offset-2 ring-offset-card")}
            data-testid="agent-colour-custom"
          />
        </label>
      </div>
      <p className="text-xs text-pretty text-muted-foreground">{t("hint")}</p>
    </fieldset>
  );
}

/**
 * A button that opens the file picker for an avatar: a PNG, JPEG or WebP image of at most 512 KiB, checked before it
 * is handed to `onPick`; what is wrong with another file is said under the button.
 */
export function AvatarPicker({ label, onPick, disabled, busy, testId = "agent-avatar-pick" }: { label: string; onPick: (file: File) => void; disabled?: boolean; busy?: boolean; testId?: string }) {
  const t = useTranslations("agents.form.avatar");
  const input = useRef<HTMLInputElement>(null);
  const [problem, setProblem] = useState<"type" | "size" | null>(null);
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <input
        ref={input}
        type="file"
        accept={AVATAR_TYPES.join(",")}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = ""; // the same file picked again is a new pick
          if (!file) return;
          const why = avatarProblem(file);
          setProblem(why);
          if (why === null) onPick(file);
        }}
        data-testid={`${testId}-input`}
      />
      <Button type="button" variant="outline" onClick={() => input.current?.click()} disabled={disabled} busy={busy} className="w-fit" data-testid={testId}>
        <ImageUp aria-hidden="true" />
        {label}
      </Button>
      {problem ? <FieldError text={problem === "type" ? t("type") : t("size", { max: AVATAR_MAX_BYTES / 1024 })} testId="agent-avatar-problem" /> : null}
    </div>
  );
}
