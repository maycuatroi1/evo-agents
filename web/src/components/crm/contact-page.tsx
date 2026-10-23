"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { When } from "@/components/data/when";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { addNote, type CrmContactDetail, contactsHref, crmContactQuery, crmKeys, deleteContact } from "./api";
import { ReaderNote, StageBadge, type CrmRole, useCrmRole, useKindWord } from "./chrome";

/** The longest note the hub keeps (evo_agents.hub.tables_crm.MAX_NOTE_CHARS). */
const MAX_NOTE = 1000;

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <Card role="region" aria-labelledby={`crm-${id}-title`} data-testid={`crm-${id}`}>
      <CardHeader>
        <CardTitle id={`crm-${id}-title`} className="text-[15px] leading-[22px] font-semibold">
          <h2>{title}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="min-w-0">{children}</CardContent>
    </Card>
  );
}

/** A writer's note on the contact: kept as a touchpoint, and read again with the contact once the hub has it. */
function NoteForm({ project, login }: { project: string; login: string }) {
  const t = useTranslations("crm.detail");
  const id = useId();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [note, setNote] = useState("");
  const [error, setError] = useState<WriteFailure | null>(null);
  const save = useMutation({
    mutationFn: () => addNote(browserApi(), project, login, note.trim()),
    onSuccess: async () => {
      setNote("");
      setError(null);
      notify({ tone: "success", text: t("noted") });
      await queryClient.invalidateQueries({ queryKey: crmKeys.contact(project, login) });
    },
    onError: (cause) => setError(failure(cause)),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (note.trim()) save.mutate();
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 border-t pt-4" data-testid="crm-note-form">
      <Label htmlFor={id}>{t("noteLabel")}</Label>
      <Textarea id={id} value={note} maxLength={MAX_NOTE} onChange={(event) => setNote(event.target.value)} rows={3} data-testid="crm-note" />
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <Button type="submit" variant="secondary" className="self-start" disabled={!note.trim()} aria-busy={save.isPending} data-testid="crm-note-save">
        {save.isPending ? t("noting") : t("addNote")}
      </Button>
    </form>
  );
}

/** Delete the contact on that person's request, for an admin: confirmed first, then back to the list. */
function DeleteContact({ project, login }: { project: string; login: string }) {
  const t = useTranslations("crm.detail");
  const router = useRouter();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const remove = useMutation({
    mutationFn: () => deleteContact(browserApi(), project, login),
    onSuccess: async () => {
      setConfirming(false);
      queryClient.removeQueries({ queryKey: crmKeys.contact(project, login) });
      await queryClient.invalidateQueries({ queryKey: crmKeys.contacts(project), exact: true });
      notify({ tone: "success", text: t("deleted", { login }) });
      router.push(contactsHref(project));
    },
    onError: (cause) => setError(failure(cause, { 403: t("deleteForbidden") })),
  });
  return (
    <>
      <Button
        type="button"
        variant="quiet-danger"
        onClick={() => {
          setError(null);
          setConfirming(true);
        }}
        data-testid="crm-delete"
      >
        <Trash2 aria-hidden="true" />
        {t("delete")}
      </Button>
      <ConfirmAction
        open={confirming}
        onOpenChange={setConfirming}
        title={t("deleteTitle", { login })}
        description={t("deleteText")}
        confirmLabel={t("deleteConfirm")}
        pendingLabel={t("deleting")}
        cancelLabel={t("keep")}
        pending={remove.isPending}
        error={error}
        onConfirm={() => remove.mutate()}
        testId="crm-delete-confirm"
      />
    </>
  );
}

function Detail({ project, contact, role }: { project: string; contact: CrmContactDetail; role: CrmRole | null }) {
  const t = useTranslations("crm");
  const word = useKindWord();
  const touchpoints = [...contact.touchpoints].sort((a, b) => b.happened_at.localeCompare(a.happened_at));
  return (
    <>
      <PageHeader
        title={<span className="font-mono">{contact.login}</span>}
        status={<StageBadge stage={contact.stage} />}
        sub={t("detail.sub", { source: word(contact.source) })}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button asChild variant="secondary">
              <a href={contact.profile_url} target="_blank" rel="noreferrer" data-testid="crm-profile">
                <ExternalLink aria-hidden="true" />
                {t("detail.profile")}
              </a>
            </Button>
            {role === "admin" ? <DeleteContact project={project} login={contact.login} /> : null}
          </div>
        }
        testId="crm-contact-header"
      />
      {role === "reader" ? <ReaderNote project={project} /> : null}
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Section id="touchpoints" title={t("detail.touchpoints")}>
          {touchpoints.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">{t("detail.noTouchpoints")}</p>
          ) : (
            <ol className="divide-y">
              {touchpoints.map((point) => (
                <li key={`${point.kind}-${point.ref}`} className="flex flex-col gap-1 py-2" data-testid="crm-touchpoint">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <span className="font-medium">{word(point.kind)}</span>
                    {point.url ? (
                      <a href={point.url} target="_blank" rel="noreferrer" className="font-mono text-[13px] text-brand underline underline-offset-4 [overflow-wrap:anywhere] hover:text-brand-hover">
                        {point.ref}
                      </a>
                    ) : point.kind === "note" ? null : (
                      <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{point.ref}</span>
                    )}
                    <span className="ml-auto text-[13px] text-muted-foreground">
                      <When value={point.happened_at} short />
                    </span>
                  </div>
                  {point.note ? (
                    <p className="text-[13px] whitespace-pre-wrap [overflow-wrap:anywhere]">
                      {point.note}
                      {point.author ? <span className="text-muted-foreground"> {t("detail.by", { author: point.author })}</span> : null}
                    </p>
                  ) : null}
                </li>
              ))}
            </ol>
          )}
          {role === "writer" || role === "admin" ? <NoteForm project={project} login={contact.login} /> : null}
        </Section>
        <Section id="history" title={t("detail.history")}>
          <ol className="flex flex-col gap-2">
            {contact.history.map((step) => (
              <li key={step.stage} className="flex flex-wrap items-center justify-between gap-2" data-testid="crm-history-step">
                <StageBadge stage={step.stage} />
                <span className="text-[13px] text-muted-foreground">
                  <When value={step.reached_at} short />
                </span>
              </li>
            ))}
          </ol>
        </Section>
      </div>
    </>
  );
}

/**
 * One contact (/p/{project}/x/crm/contacts/{login}): its stage and source, its touchpoints newest first, the stages it
 * reached, a writer's note, and an admin's Delete on that person's request.
 */
export function ContactPage({ project, login, initialError }: { project: string; login: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("crm");
  const role = useCrmRole(project);
  const state = useHubQuery(crmContactQuery(browserApi, project, login), initialError, { live: true });
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="crm-contact-page">
      <Link href={contactsHref(project)} className="inline-flex min-h-11 w-fit items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground md:min-h-0" data-testid="crm-back">
        <ArrowLeft aria-hidden="true" className="size-4" />
        {t("back")}
      </Link>
      <QueryView state={state}>{(contact) => <Detail project={project} contact={contact} role={role} />}</QueryView>
    </div>
  );
}
