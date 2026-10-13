"use client";

import { useTranslations } from "next-intl";
import type { FormEvent, ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { type Failure, FailureBox } from "./hooks";

/**
 * The frame of the Accounts page's two forms, an account's weight and priority and a project's policy: a dialog that
 * cannot be closed while its write runs, its title and sentence, the fields, the hub's refusal, Cancel and Save. Its
 * content mounts each time it opens, so a new opening starts from what the hub holds.
 */
export function FormDialog({
  open,
  onOpenChange,
  title,
  description,
  pending,
  failure,
  labels,
  onSubmit,
  testId,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  pending: boolean;
  failure: Failure | null;
  labels: { save: string; saving: string; cancel: string };
  onSubmit: () => void;
  testId: string;
  children: ReactNode;
}) {
  const t = useTranslations("accounts.errors");
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
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="text-pretty">{description}</DialogDescription>
        </DialogHeader>
        <form className="flex flex-col gap-5" onSubmit={submit} noValidate>
          {children}
          {failure ? <FailureBox failure={failure} requestLabel={t("requestId", { id: failure.requestId ?? "" })} /> : null}
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
                {labels.cancel}
              </Button>
            </DialogClose>
            <Button type="submit" size="lg" busy={pending} data-testid={`${testId}-save`}>
              {pending ? labels.saving : labels.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** A whole number field with its hint and, once the form was sent, its problem. */
export function NumberField({
  id,
  label,
  hint,
  problem,
  value,
  onChange,
  testId,
}: {
  id: string;
  label: string;
  hint: string;
  problem: string | null;
  value: string;
  onChange: (value: string) => void;
  testId: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="text"
        inputMode="numeric"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
        className="h-9 w-full tabular-nums sm:w-40"
        aria-invalid={problem ? true : undefined}
        aria-describedby={problem ? `${id}-hint ${id}-error` : `${id}-hint`}
        data-testid={testId}
      />
      <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
        {hint}
      </p>
      {problem ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger" data-testid={`${testId}-error`}>
          {problem}
        </p>
      ) : null}
    </div>
  );
}
