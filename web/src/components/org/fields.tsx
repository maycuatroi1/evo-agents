"use client";

import { useTranslations } from "next-intl";
import type { FormEvent, ReactNode } from "react";

import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import type { Problem } from "./form-model";
import type { Level } from "./queries";

/**
 * The parts the Org page's dialogs share: a dialog that holds one form and stays open while it saves, a field with its
 * label, hint and problem tied to it by id, a level picker and a login box that suggests the logins the hub knows.
 */

type FormDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  submitLabel: string;
  pending: boolean;
  error: WriteFailure | null;
  onSubmit: () => void;
  testId: string;
  children: ReactNode;
};

export function FormDialog({ open, onOpenChange, title, description, submitLabel, pending, error, onSubmit, testId, children }: FormDialogProps) {
  const t = useTranslations("org");
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!pending) onSubmit();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-lg"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid={testId}
      >
        <DialogHeader>
          <DialogTitle className="pr-8 text-balance break-words">{title}</DialogTitle>
          {description ? <DialogDescription className="text-pretty">{description}</DialogDescription> : null}
        </DialogHeader>
        <form onSubmit={submit} noValidate className="flex min-w-0 flex-col gap-4" aria-busy={pending || undefined}>
          {children}
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <DialogFooter>
            <DialogClose asChild>
              <Button
                type="button"
                variant="outline"
                size="lg"
                aria-disabled={pending || undefined}
                className="aria-disabled:opacity-50"
                onClick={(event) => pending && event.preventDefault()}
              >
                {t("cancel")}
              </Button>
            </DialogClose>
            <Button type="submit" size="lg" busy={pending} data-testid={`${testId}-save`}>
              {pending ? t("saving") : submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** A field: its label, the control, a hint and the problem, the last two named by the control's aria-describedby. */
export function Field({
  id,
  label,
  hint,
  problem,
  children,
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  problem?: Problem;
  children: (described: { id: string; "aria-describedby"?: string; "aria-invalid"?: true }) => ReactNode;
}) {
  const t = useTranslations("org.problems");
  const describedBy = [hint ? `${id}-hint` : null, problem ? `${id}-error` : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children({ id, "aria-describedby": describedBy, ...(problem ? { "aria-invalid": true as const } : {}) })}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {problem ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger" data-testid="org-field-problem">
          {t(problem)}
        </p>
      ) : null}
    </div>
  );
}

/** A level from those the viewer may set; `none` adds an empty choice with that label. */
export function LevelSelect({
  value,
  levels,
  onChange,
  none,
  ...control
}: {
  id: string;
  value: Level | "";
  levels: readonly Level[];
  onChange: (level: Level | "") => void;
  none?: string;
  "aria-describedby"?: string;
}) {
  const t = useTranslations("org.levels");
  return (
    <NativeSelect value={value} onChange={(event) => onChange(event.target.value as Level | "")} className="w-full" data-testid="org-level" {...control}>
      {none ? <NativeSelectOption value="">{none}</NativeSelectOption> : null}
      {levels.map((level) => (
        <NativeSelectOption key={level} value={level}>
          {t(level)}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}

/** A login, with the logins the hub knows as suggestions. */
export function LoginInput({
  value,
  onChange,
  logins,
  testId,
  ...control
}: {
  id: string;
  value: string;
  onChange: (login: string) => void;
  logins: readonly string[];
  testId: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
  "aria-required"?: true;
}) {
  return (
    <>
      <Input
        {...control}
        value={value}
        onChange={(event) => onChange(event.target.value.trim())}
        list={`${control.id}-logins`}
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        maxLength={100}
        className="h-9 font-mono"
        data-testid={testId}
      />
      <datalist id={`${control.id}-logins`}>
        {logins.map((login) => (
          <option key={login} value={login} />
        ))}
      </datalist>
    </>
  );
}
