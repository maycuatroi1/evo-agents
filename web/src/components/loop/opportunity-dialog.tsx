"use client";

import { useQueryClient } from "@tanstack/react-query";
import { PencilLine, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { InlineError } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { browserApi } from "@/lib/api/browser";
import { opportunitiesHref, opportunitiesQuery, type ProjectOpportunity } from "@/lib/opportunity-queries";
import { nextOpportunityCode, writeOpportunity } from "@/lib/opportunity-writes";

import { OpportunityFields } from "./opportunity-fields";
import { useOpportunityWrite } from "./opportunity-hooks";
import { EMPTY_OPPORTUNITY, formOf, formProblems, type OpportunityForm, writeOf } from "./opportunity-model";

type Written = Awaited<ReturnType<typeof writeOpportunity>>;

/**
 * The form of an opportunity in a dialog: its fields, the refusal of the hub beside its buttons, Cancel and the submit
 * button, busy while the write runs. `held` is the opportunity an edit starts from, at the revision the page read.
 */
function OpportunityFormBody({
  held,
  title,
  description,
  submit,
  busyLabel,
  busy,
  refused,
  onSubmit,
  testId,
}: {
  held: ProjectOpportunity | null;
  title: string;
  description: string;
  submit: string;
  busyLabel: string;
  busy: boolean;
  refused: { text: string; detail: string | null; requestId: string | null } | null;
  onSubmit: (form: OpportunityForm) => void;
  testId: string;
}) {
  const t = useTranslations("opportunities.form");
  const [form, setForm] = useState<OpportunityForm>(() => (held ? formOf(held) : EMPTY_OPPORTUNITY));
  const [tried, setTried] = useState(false);
  const wrong = tried ? formProblems(form) : [];
  const send = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (formProblems(form).length > 0 || busy) return;
    onSubmit(form);
  };
  const Icon = held ? PencilLine : Plus;
  return (
    <form onSubmit={send} noValidate className="flex flex-col gap-5" data-testid={testId}>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
          {title}
        </DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <OpportunityFields form={form} onChange={setForm} wrong={wrong} isNew={held === null} />
      {wrong.length > 0 ? (
        <p role="alert" className="text-[13px] font-medium text-danger" data-testid="opportunity-form-wrong">
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
        <Button type="submit" size="lg" busy={busy} data-testid={`${testId}-submit`}>
          {busy ? busyLabel : submit}
        </Button>
      </DialogFooter>
    </form>
  );
}

/**
 * A writer adds an opportunity: it takes the project's next code (read again just before the write, since a code is never
 * given twice) and only creates one (`if_revision` 0), so two writers adding at once never overwrite each other; the
 * page then opens the new opportunity.
 */
export function NewOpportunityDialog({
  project,
  opportunities,
  open,
  onOpenChange,
}: {
  project: string;
  opportunities: readonly ProjectOpportunity[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("opportunities.form");
  const router = useRouter();
  const queryClient = useQueryClient();
  const { busy, refused, run, clear } = useOpportunityWrite<Written>(project);
  const create = (form: OpportunityForm) =>
    void run(
      async () => {
        const fresh = await queryClient.fetchQuery({ ...opportunitiesQuery(browserApi, project), staleTime: 0 }).catch(() => opportunities);
        return writeOpportunity(browserApi(), project, nextOpportunityCode(fresh), writeOf(form, null));
      },
      (written) => {
        notify({ tone: "success", text: t("created", { code: written.code }), link: { label: t("open"), href: opportunitiesHref(project, written.code) } });
        onOpenChange(false);
        router.push(opportunitiesHref(project, written.code));
      },
      { 409: t("createConflict"), 422: t("refused") },
    );
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return;
        clear();
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl" data-testid="opportunity-new-dialog">
        {open ? (
          <OpportunityFormBody
            held={null}
            title={t("newTitle")}
            description={t("newText")}
            submit={t("create")}
            busyLabel={t("creating")}
            busy={busy}
            refused={refused}
            onSubmit={create}
            testId="opportunity-new-form"
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

/**
 * A writer edits an opportunity whole, from the revision the page read: its status and the reason it was closed stay as
 * they are (Close and Reopen change those). A write equal to the opportunity now writes nothing and says so; a 409 says
 * another writer wrote it since, and the page shows their revision.
 */
export function EditOpportunityDialog({
  project,
  opportunity,
  open,
  onOpenChange,
}: {
  project: string;
  opportunity: ProjectOpportunity;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("opportunities.form");
  const { busy, refused, run, clear } = useOpportunityWrite<Written>(project);
  const save = (form: OpportunityForm) =>
    void run(
      () => writeOpportunity(browserApi(), project, opportunity.code, writeOf(form, opportunity)),
      (written) => {
        notify({
          tone: written.changed ? "success" : "info",
          text: written.changed ? t("saved", { code: written.code, revision: written.revision }) : t("unchanged", { code: written.code }),
        });
        onOpenChange(false);
      },
      { 409: t("editConflict", { code: opportunity.code }), 422: t("refused") },
    );
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return;
        clear();
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl" data-testid="opportunity-edit-dialog">
        {open ? (
          <OpportunityFormBody
            key={opportunity.revision}
            held={opportunity}
            title={t("editTitle", { code: opportunity.code })}
            description={t("editText", { revision: opportunity.revision })}
            submit={t("save")}
            busyLabel={t("saving")}
            busy={busy}
            refused={refused}
            onSubmit={save}
            testId="opportunity-edit-form"
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
