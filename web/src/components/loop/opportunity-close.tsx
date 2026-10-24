"use client";

import { CircleCheck, CircleMinus, CircleX, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import type { ProjectOpportunity } from "@/lib/opportunity-queries";
import { CLOSED_STATUSES, closeOpportunity, OPPORTUNITY_LIMITS, reopening, writeOpportunity } from "@/lib/opportunity-writes";

import { useOpportunityWrite } from "./opportunity-hooks";

type Closed = (typeof CLOSED_STATUSES)[number];
type Written = Awaited<ReturnType<typeof writeOpportunity>>;

const CLOSE_ICONS = { captured: CircleCheck, missed: CircleX, dropped: CircleMinus } as const;

function CloseForm({ project, opportunity, onClose }: { project: string; opportunity: ProjectOpportunity; onClose: () => void }) {
  const t = useTranslations("opportunities.close");
  const words = useTranslations("opportunities.status");
  const ids = useId();
  const [status, setStatus] = useState<Closed>("captured");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [tried, setTried] = useState(false);
  const { busy, refused, run } = useOpportunityWrite<Written>(project);
  const missing = tried && !reason.trim();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!reason.trim() || busy) return;
    void run(
      () => closeOpportunity(browserApi(), project, opportunity.code, { status, reason: reason.trim(), note: note.trim() || null, if_revision: opportunity.revision }),
      (written) => {
        notify({ tone: "success", text: t("done", { code: written.code, status: written.status }) });
        onClose();
      },
      { 409: t("conflict", { code: opportunity.code }), 422: t("refused") },
    );
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5" data-testid="opportunity-close-form">
      <DialogHeader>
        <DialogTitle>{t("title", { code: opportunity.code })}</DialogTitle>
        <DialogDescription>{t("text")}</DialogDescription>
      </DialogHeader>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1.5 text-sm font-medium">{t("how")}</legend>
        <RadioGroup value={status} onValueChange={(value) => setStatus(CLOSED_STATUSES.find((one) => one === value) ?? "captured")} name="status" data-testid="opportunity-close-status">
          {CLOSED_STATUSES.map((value) => {
            const Icon = CLOSE_ICONS[value];
            return (
              <Label
                key={value}
                htmlFor={`${ids}-${value}`}
                className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 font-normal transition-colors hover:bg-accent has-data-checked:border-brand/40 has-data-checked:bg-surface-selected"
              >
                <RadioGroupItem id={`${ids}-${value}`} value={value} className="mt-0.5" aria-labelledby={`${ids}-${value}-name`} aria-describedby={`${ids}-${value}-hint`} data-testid={`opportunity-close-${value}`} />
                <span className="flex flex-col gap-1">
                  <span id={`${ids}-${value}-name`} className="inline-flex items-center gap-1.5 text-sm font-medium">
                    <Icon className="size-3.5 text-muted-foreground" aria-hidden="true" />
                    {words(value)}
                  </span>
                  <span id={`${ids}-${value}-hint`} className="text-xs leading-snug text-muted-foreground">
                    {t(`hints.${value}`)}
                  </span>
                </span>
              </Label>
            );
          })}
        </RadioGroup>
      </fieldset>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-reason`}>
          {t("reason")}
          <span className="font-normal text-fg-subtle">{t("required")}</span>
        </Label>
        <Textarea
          id={`${ids}-reason`}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          rows={3}
          maxLength={OPPORTUNITY_LIMITS.reason}
          aria-required="true"
          aria-invalid={missing || undefined}
          aria-describedby={`${ids}-reason-${missing ? "error" : "help"}`}
          data-testid="opportunity-close-reason-input"
        />
        {missing ? (
          <p id={`${ids}-reason-error`} className="text-xs font-medium text-danger">
            {t("reasonMissing")}
          </p>
        ) : (
          <p id={`${ids}-reason-help`} className="text-xs text-fg-subtle">
            {t("reasonHelp")}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-note`}>{t("note")}</Label>
        <Input id={`${ids}-note`} value={note} onChange={(event) => setNote(event.target.value)} maxLength={OPPORTUNITY_LIMITS.note} />
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="opportunity-close-submit">
          {busy ? t("closing") : t("submit", { status })}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** A writer closes an open opportunity as captured, missed or dropped, saying why; it stays, with its reason, and may be reopened. */
export function CloseOpportunityDialog({ project, opportunity, open, onOpenChange }: { project: string; opportunity: ProjectOpportunity; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg" data-testid="opportunity-close-dialog">
        {open ? <CloseForm project={project} opportunity={opportunity} onClose={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function ReopenForm({ project, opportunity, onClose }: { project: string; opportunity: ProjectOpportunity; onClose: () => void }) {
  const t = useTranslations("opportunities.reopen");
  const ids = useId();
  const [note, setNote] = useState("");
  const { busy, refused, run } = useOpportunityWrite<Written>(project);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    void run(
      () => writeOpportunity(browserApi(), project, opportunity.code, reopening(opportunity, note)),
      (written) => {
        notify({ tone: "success", text: t("done", { code: written.code }) });
        onClose();
      },
      { 409: t("conflict", { code: opportunity.code }), 422: t("refused") },
    );
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5" data-testid="opportunity-reopen-form">
      <DialogHeader>
        <DialogTitle>{t("title", { code: opportunity.code })}</DialogTitle>
        <DialogDescription>{t("text", { status: opportunity.status, reason: opportunity.close_reason ?? "" })}</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-note`}>{t("note")}</Label>
        <Input id={`${ids}-note`} value={note} onChange={(event) => setNote(event.target.value)} maxLength={OPPORTUNITY_LIMITS.note} data-testid="opportunity-reopen-note" />
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="opportunity-reopen-submit">
          <RotateCcw aria-hidden="true" />
          {busy ? t("reopening") : t("submit")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** A writer reopens a closed opportunity: the same opportunity, open again, without the reason it was closed. */
export function ReopenOpportunityDialog({ project, opportunity, open, onOpenChange }: { project: string; opportunity: ProjectOpportunity; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md" data-testid="opportunity-reopen-dialog">
        {open ? <ReopenForm project={project} opportunity={opportunity} onClose={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}
