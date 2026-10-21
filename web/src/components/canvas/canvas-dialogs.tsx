"use client";

import { Table } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { createCanvas } from "./api";
import { useCanvasWrite } from "./hooks";
import { canvasHref } from "./links";
import { LIMITS, SLUG, slugOf } from "./model";

function NewCanvasForm({ project, onClose }: { project: string; onClose: () => void }) {
  const t = useTranslations("canvas.newCanvas");
  const ids = useId();
  const router = useRouter();
  const { busy, refused, run } = useCanvasWrite(project, null);
  const [form, setForm] = useState({ name: "", slug: "", description: "", slugTouched: false });
  const [tried, setTried] = useState(false);
  const slug = form.slugTouched ? form.slug : slugOf(form.name);
  const nameMissing = tried && !form.name.trim();
  const slugBad = tried && !SLUG.test(slug);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!form.name.trim() || !SLUG.test(slug) || busy) return;
    void run(
      async () => {
        await createCanvas(browserApi(), project, { slug, name: form.name.trim(), description: form.description.trim() });
        return t("created", { name: form.name.trim() });
      },
      () => {
        onClose();
        router.push(canvasHref(project, slug));
      },
    );
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5" data-testid="canvas-new-form">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <Table className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("title")}
        </DialogTitle>
        <DialogDescription>{t("text")}</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
        <Input
          id={`${ids}-name`}
          value={form.name}
          onChange={(event) => setForm((held) => ({ ...held, name: event.target.value }))}
          maxLength={LIMITS.name}
          placeholder={t("namePlaceholder")}
          aria-required="true"
          aria-invalid={nameMissing || undefined}
          aria-describedby={nameMissing ? `${ids}-name-error` : undefined}
          data-testid="canvas-new-name"
        />
        {nameMissing ? (
          <p id={`${ids}-name-error`} className="text-xs font-medium text-danger">
            {t("nameMissing")}
          </p>
        ) : null}
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-slug`}>{t("slug")}</Label>
        <Input
          id={`${ids}-slug`}
          value={slug}
          onChange={(event) => setForm((held) => ({ ...held, slug: event.target.value, slugTouched: true }))}
          maxLength={100}
          className="font-mono"
          aria-invalid={slugBad || undefined}
          aria-describedby={`${ids}-slug-help`}
          data-testid="canvas-new-slug"
        />
        <p id={`${ids}-slug-help`} className={slugBad ? "text-xs font-medium text-danger" : "text-xs text-fg-subtle"}>
          {slugBad ? t("slugBad") : t("slugHelp")}
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-description`}>{t("description")}</Label>
        <Textarea
          id={`${ids}-description`}
          value={form.description}
          onChange={(event) => setForm((held) => ({ ...held, description: event.target.value }))}
          rows={3}
          maxLength={LIMITS.description}
        />
      </div>
      {refused ? <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="canvas-new-create">
          {t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** A writer starts the canvas of a product: its name, the slug its pages go by, and what the product is. */
export function NewCanvasDialog({ project, open, onOpenChange }: { project: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        {open ? <NewCanvasForm project={project} onClose={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}
