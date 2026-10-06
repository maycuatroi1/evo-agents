"use client";

import { type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/admin/notice";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** What has to be typed, exactly: the worker's name. */
  name: string;
  icon: LucideIcon;
  tone: "warning" | "danger";
  title: string;
  description: ReactNode;
  confirmLabel: string;
  pendingLabel: string;
  pending: boolean;
  error: WriteFailure | null;
  onConfirm: () => void;
  testId: string;
};

/**
 * A confirmation that asks for the worker's name, typed out, before it drains or revokes the worker: a click alone,
 * or Enter on a focused button, cannot stop a machine. Focus starts in the field; confirming with another name says
 * what to type instead and changes nothing. While the write runs the dialog cannot be dismissed, and a refusal shows
 * inside it. The field starts empty each time the dialog opens.
 */
export function ConfirmByName({ open, onOpenChange, pending, ...rest }: Props) {
  const ids = useId();
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <AlertDialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <AlertDialogContent
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md"
        onEscapeKeyDown={guard}
        onOpenAutoFocus={(event) => {
          // An alert dialog focuses Cancel; here the first thing to do is type the name.
          event.preventDefault();
          document.getElementById(`${ids}-name`)?.focus();
        }}
        data-testid={rest.testId}
      >
        <ConfirmForm ids={ids} pending={pending} {...rest} />
      </AlertDialogContent>
    </AlertDialog>
  );
}

function ConfirmForm({
  name,
  icon: Icon,
  tone,
  title,
  description,
  confirmLabel,
  pendingLabel,
  pending,
  error,
  onConfirm,
  testId,
  ids,
}: Omit<Props, "open" | "onOpenChange"> & { ids: string }) {
  const t = useTranslations("workers.confirm");
  const [typed, setTyped] = useState("");
  const [tried, setTried] = useState(false);
  const matches = typed.trim() === name;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    if (!matches) {
      setTried(true);
      return;
    }
    onConfirm();
  };

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4" aria-busy={pending || undefined}>
      <AlertDialogHeader>
        <AlertDialogMedia
          className={tone === "danger" ? "bg-danger-soft text-danger" : "bg-attention-soft text-attention"}
        >
          <Icon aria-hidden="true" />
        </AlertDialogMedia>
        <AlertDialogTitle className="text-balance break-words">{title}</AlertDialogTitle>
        <AlertDialogDescription asChild>
          <div className="flex flex-col gap-2 text-left">{description}</div>
        </AlertDialogDescription>
      </AlertDialogHeader>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-name`} className="block leading-normal">
          {t.rich("typeName", { name, code: (chunks) => <code className="font-mono font-medium break-all">{chunks}</code> })}
        </Label>
        <Input
          id={`${ids}-name`}
          value={typed}
          onChange={(event) => {
            setTyped(event.target.value);
            setTried(false);
          }}
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          maxLength={120}
          className="h-9 font-mono"
          aria-invalid={tried && !matches ? true : undefined}
          aria-describedby={tried && !matches ? `${ids}-error` : undefined}
          data-testid={`${testId}-name`}
        />
        {tried && !matches ? (
          <p id={`${ids}-error`} className="text-xs font-medium text-danger">
            {t("mismatch", { name })}
          </p>
        ) : null}
      </div>
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel
          size="lg"
          aria-disabled={pending || undefined}
          className="aria-disabled:opacity-50"
          onClick={(event) => pending && event.preventDefault()}
        >
          {t("cancel")}
        </AlertDialogCancel>
        <Button
          type="submit"
          variant={tone === "danger" ? "destructive" : "default"}
          size="lg"
          busy={pending}
          data-testid={`${testId}-confirm`}
        >
          {pending ? pendingLabel : confirmLabel}
        </Button>
      </div>
    </form>
  );
}
