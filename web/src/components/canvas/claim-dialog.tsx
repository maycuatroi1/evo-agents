"use client";

import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";

import { type Cell, editClaims } from "./api";
import { ClaimFields } from "./claim-fields";
import { useCanvasWrite } from "./hooks";
import { changeOf, type ClaimForm, emptyForm, LIMITS, type SignalKeyOption, testCardOf, testingLacks } from "./model";

function NewClaimForm({
  project,
  slug,
  cell,
  keys,
  onClose,
}: {
  project: string;
  slug: string;
  cell: Cell;
  keys: readonly SignalKeyOption[];
  onClose: () => void;
}) {
  const t = useTranslations("canvas.newClaim");
  const tCell = useTranslations("canvas.cell");
  const { busy, refused, run } = useCanvasWrite(project, slug);
  const [form, setForm] = useState<ClaimForm>(() => emptyForm(cell));
  const [reason, setReason] = useState("");
  const [tried, setTried] = useState(false);
  const ids = useId();
  const textMissing = tried && !form.text.trim();
  const lacks = form.status === "testing" ? testingLacks(testCardOf(form)) : [];
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    const change = changeOf(form, null);
    if (!form.text.trim() || lacks.length > 0 || !change || busy) return;
    void run(async () => {
      const done = await editClaims(browserApi(), project, slug, [change], reason.trim());
      return t("created", { code: done.written[0] ?? "", cell: tCell(form.cell) });
    }, onClose);
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5" data-testid="claim-new-form">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <Plus className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("title", { cell: tCell(form.cell) })}
        </DialogTitle>
        <DialogDescription>{t("text")}</DialogDescription>
      </DialogHeader>
      <ClaimFields form={form} onChange={setForm} keys={keys} textMissing={textMissing} lacks={tried ? lacks : []} />
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-reason`}>{t("reason")}</Label>
        <Input id={`${ids}-reason`} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={LIMITS.reason} placeholder={t("reasonPlaceholder")} />
      </div>
      {refused ? <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="claim-new-create">
          {t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** A writer adds a claim to a cell: its text, status and confidence, a test card and what it links; it starts at rev 1. */
export function NewClaimDialog({
  project,
  slug,
  cell,
  keys,
  onOpenChange,
}: {
  project: string;
  slug: string;
  cell: Cell | null;
  keys: readonly SignalKeyOption[];
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={cell !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl">
        {cell ? <NewClaimForm project={project} slug={slug} cell={cell} keys={keys} onClose={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}
