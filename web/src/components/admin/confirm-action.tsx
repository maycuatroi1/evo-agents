"use client";

import { Loader2, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

import { InlineError, type WriteFailure } from "./notice";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  pendingLabel: string;
  cancelLabel: string;
  pending: boolean;
  error: WriteFailure | null;
  onConfirm: () => void;
  testId: string;
};

/**
 * The confirmation every destructive admin action goes through. Focus starts on Cancel, so Enter does not destroy
 * anything by accident; the dialog stays open while the write runs (Escape and clicks outside are ignored), and a
 * failure shows inside it. The caller closes it on success.
 */
export function ConfirmAction({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  pendingLabel,
  cancelLabel,
  pending,
  error,
  onConfirm,
  testId,
}: Props) {
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <AlertDialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <AlertDialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md" onEscapeKeyDown={guard} data-testid={testId}>
        <AlertDialogHeader>
          <AlertDialogMedia className="bg-destructive/10 text-destructive">
            <TriangleAlert aria-hidden="true" />
          </AlertDialogMedia>
          <AlertDialogTitle className="text-balance break-words">{title}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="flex flex-col gap-2 text-left">{description}</div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel size="lg" aria-disabled={pending || undefined} className="aria-disabled:opacity-50" onClick={(event) => pending && event.preventDefault()}>
            {cancelLabel}
          </AlertDialogCancel>
          <Button
            type="button"
            size="lg"
            className="bg-destructive text-primary-foreground hover:bg-destructive/90"
            aria-disabled={pending || undefined}
            onClick={() => {
              if (!pending) onConfirm();
            }}
            data-testid={`${testId}-confirm`}
          >
            {pending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}
            {pending ? pendingLabel : confirmLabel}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
