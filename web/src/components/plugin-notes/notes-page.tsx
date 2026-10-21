"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { NotebookPen, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { addNote, MAX_NOTE_CHARS, notesKey, notesQuery } from "./api";

/**
 * The native page of the core plugin notes, the tests' core plugin (tests/hub/core_notes.py): the project's notes,
 * newest last, and a box that adds one. It shows how a core plugin's page lives in the web (web/DESIGN.md, Plugins):
 * its own feature folder, a route under src/app/(app)/p/[project]/x/<plugin> whose layout calls requirePlugin, and its
 * plugin's routes for its data. The web's end-to-end build carries it (EVO_WEB_FIXTURES); a production build does not.
 */
export function NotesPage({ project, title }: { project: string; title: string }) {
  const t = useTranslations("plugins.notes");
  const ids = useId();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const state = useHubQuery(notesQuery(project));
  const [text, setText] = useState("");
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const add = useMutation({
    mutationFn: (body: string) => addNote(browserApi(), project, body),
    onSettled: () => queryClient.invalidateQueries({ queryKey: notesKey(project) }),
  });

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const body = text.trim();
    if (!body || add.isPending) return;
    setRefused(null);
    try {
      await add.mutateAsync(body);
      setText("");
      notify({ tone: "success", text: t("added") });
    } catch (error) {
      setRefused(failure(error));
    }
  };

  return (
    <div className="flex flex-col gap-6" data-testid="notes-page">
      <PageHeader
        title={title}
        tags={
          <Tag>
            <NotebookPen aria-hidden="true" />
            {t("tag")}
          </Tag>
        }
      />
      <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <QueryView state={state} loading={<ListSkeleton rows={3} />}>
          {(notes) =>
            notes.length === 0 ? (
              <EmptyState icon={NotebookPen} title={t("emptyTitle")} description={t("emptyDescription")} testId="notes-empty" />
            ) : (
              <Card className="min-w-0">
                <CardContent>
                  <ol className="flex flex-col divide-y" aria-label={t("listLabel", { project })} data-testid="notes-list">
                    {notes.map((note, index) => (
                      <li key={`${index}-${note.author}`} className="flex flex-col gap-0.5 py-2.5 first:pt-0 last:pb-0">
                        <p className="text-sm text-pretty [overflow-wrap:anywhere]">{note.body}</p>
                        <p className="text-xs text-fg-subtle">{t("by", { login: note.author })}</p>
                      </li>
                    ))}
                  </ol>
                </CardContent>
              </Card>
            )
          }
        </QueryView>
        <Card className="min-w-0 self-start">
          <CardContent>
            <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-3" data-testid="notes-form">
              <Label htmlFor={`${ids}-note`}>{t("newNote")}</Label>
              <Textarea
                id={`${ids}-note`}
                value={text}
                onChange={(event) => setText(event.target.value)}
                maxLength={MAX_NOTE_CHARS}
                rows={3}
                aria-describedby={`${ids}-hint`}
              />
              <p id={`${ids}-hint`} className="text-xs text-fg-subtle">
                {t("hint", { max: MAX_NOTE_CHARS })}
              </p>
              {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
              <Button type="submit" busy={add.isPending} disabled={!text.trim()} className="self-end" data-testid="notes-add">
                <Plus aria-hidden="true" />
                {add.isPending ? t("adding") : t("add")}
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
