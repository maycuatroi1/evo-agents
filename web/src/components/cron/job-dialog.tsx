"use client";

import { PencilLine, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { browserApi } from "@/lib/api/browser";

import { useCronWrite } from "./hooks";
import { CronJobFields } from "./job-fields";
import { browserTimeZone, bodyOf, type CronForm, emptyForm, formOf, formProblems } from "./model";
import { type CronJob, putCronJob } from "./queries";

type Written = Awaited<ReturnType<typeof putCronJob>>;

/** The form in the dialog: its fields, the hub's refusal beside the buttons, Cancel and the submit button. */
function CronJobForm({
  project,
  held,
  busy,
  refused,
  onSubmit,
}: {
  project: string;
  held: CronJob | null;
  busy: boolean;
  refused: WriteFailure | null;
  onSubmit: (form: CronForm) => void;
}) {
  const t = useTranslations("cron.form");
  const [form, setForm] = useState<CronForm>(() => (held ? formOf(held) : emptyForm(browserTimeZone())));
  const [tried, setTried] = useState(false);
  const wrong = tried ? formProblems(form, held === null) : [];
  const send = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (formProblems(form, held === null).length > 0 || busy) return;
    onSubmit(form);
  };
  const Icon = held ? PencilLine : Plus;
  return (
    <form onSubmit={send} noValidate className="flex flex-col gap-5" data-testid={held ? "cron-edit-form" : "cron-new-form"}>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
          {held ? t("editTitle", { name: held.name }) : t("newTitle")}
        </DialogTitle>
        <DialogDescription>{held ? t("editText", { revision: held.revision, owner: held.owner }) : t("newText")}</DialogDescription>
      </DialogHeader>
      <CronJobFields project={project} form={form} onChange={setForm} wrong={wrong} isNew={held === null} />
      {wrong.length > 0 ? (
        <p role="alert" className="text-[13px] font-medium text-danger" data-testid="cron-form-wrong">
          {t("wrong", { count: wrong.length })}
        </p>
      ) : null}
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="cron-form-submit">
          {held ? (busy ? t("saving") : t("save")) : busy ? t("creating") : t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/**
 * A writer makes a cron job (`if_revision` 0, so a name taken meanwhile is a 409, never an overwrite) or its owner
 * changes what it starts, from the revision the page read. On a 409 the page reads the job again: the form starts over
 * from the revision the hub holds (it is keyed by the revision), and the refusal says that nothing was written.
 */
export function CronJobDialog({
  project,
  job,
  open,
  onOpenChange,
}: {
  project: string;
  job: CronJob | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("cron.form");
  const { busy, refused, run, clear } = useCronWrite<Written>(project);
  const submit = (form: CronForm) => {
    const name = job ? job.name : form.name.trim();
    void run(
      () => putCronJob(browserApi(), project, name, bodyOf(form, job?.revision ?? 0)),
      (written) => {
        notify({
          tone: written.changed ? "success" : "info",
          text: written.created ? t("created", { name }) : written.changed ? t("saved", { name, revision: written.revision }) : t("unchanged", { name }),
        });
        onOpenChange(false);
      },
      job
        ? { 403: t("notOwner", { owner: job.owner }), 404: t("gone", { name }), 409: t("editConflict", { name }), 422: t("refused") }
        : { 409: t("createConflict", { name }), 422: t("refused") },
    );
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return;
        clear();
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl" data-testid={job ? "cron-edit-dialog" : "cron-new-dialog"}>
        {open ? <CronJobForm key={job ? `${job.name}@${job.revision}` : "new"} project={project} held={job} busy={busy} refused={refused} onSubmit={submit} /> : null}
      </DialogContent>
    </Dialog>
  );
}
