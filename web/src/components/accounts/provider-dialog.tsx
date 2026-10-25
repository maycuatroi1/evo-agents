"use client";

import { useState } from "react";

import { Dialog, DialogContent } from "@/components/ui/dialog";

import { ProviderCatalog } from "./provider-catalog";
import { ProviderFormBody, type ProviderTarget } from "./provider-form";
import type { CatalogProvider } from "./provider-queries";

/** What the dialog opens on: the catalog (Add provider), or straight on the form (a card's Add, an account's Edit). */
export type ProviderStart = { kind: "catalog" } | ProviderTarget;

/**
 * Add provider and Edit: the catalog first when the person has not picked a provider, then the form of the entry
 * they picked, with Back to the catalog. Claude is not a provider account: picking it closes this dialog and opens
 * the Claude sign-in (`onClaude`). The dialog cannot be closed while a write runs; the form says so by its own state.
 */
export function ProviderDialog({
  open,
  onOpenChange,
  start,
  onClaude,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  start: ProviderStart;
  onClaude: () => void;
}) {
  const [picked, setPicked] = useState<CatalogProvider | null>(null);
  const [busy, setBusy] = useState(false);
  // A new opening starts from the catalog again: what was picked goes with the closing.
  const change = (next: boolean) => {
    if (!next) setPicked(null);
    onOpenChange(next);
  };
  const close = () => change(false);
  const pick = (entry: CatalogProvider) => {
    if (entry.via === "login") {
      close();
      onClaude();
      return;
    }
    setPicked(entry);
  };
  const target: ProviderTarget | null = start.kind === "catalog" ? (picked ? { kind: "add", entry: picked } : null) : start;
  const guard = (event: Event) => {
    if (busy) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (busy ? undefined : change(next))}>
      <DialogContent
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-2xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="provider-dialog"
        data-step={target ? "form" : "catalog"}
      >
        {target ? (
          <ProviderFormBody
            key={target.kind === "edit" ? target.account.name : target.entry.id}
            target={target}
            onBack={start.kind === "catalog" ? () => setPicked(null) : undefined}
            onBusy={setBusy}
            onClose={close}
          />
        ) : (
          <ProviderCatalog onPick={pick} />
        )}
      </DialogContent>
    </Dialog>
  );
}
