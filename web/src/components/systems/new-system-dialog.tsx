"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { browserApi } from "@/lib/api/browser";
import { MAX_NAME_CHARS, newSpec, slugOf, slugProblem } from "@/lib/systems";
import { putSystem, systemKeys } from "@/lib/systems-queries";

type Props = { project: string; open: boolean; onOpenChange: (open: boolean) => void; onCreated: (slug: string) => void };

/**
 * New system: a name and the slug it is reached by (made of the name until edited), created as revision 1 with its
 * entry alone; the designer adds the rest. A slug taken already is the hub's 409.
 */
export function NewSystemDialog({ project, open, onOpenChange, onCreated }: Props) {
  const t = useTranslations("systems.new");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState<string | null>(null);
  const [tried, setTried] = useState(false);
  const shownSlug = slug ?? slugOf(name);
  const create = useMutation({
    mutationFn: () => putSystem(browserApi(), project, shownSlug, newSpec(name), null),
    onSuccess: (written) => {
      void queryClient.invalidateQueries({ queryKey: systemKeys.list(project) });
      queryClient.setQueryData(systemKeys.one(project, written.slug), written);
      onOpenChange(false);
      onCreated(written.slug);
    },
  });
  const nameProblem = !name.trim() ? t("nameBlank") : [...name.trim()].length > MAX_NAME_CHARS ? t("nameLong", { max: MAX_NAME_CHARS }) : null;
  const problem = slugProblem(shownSlug);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (nameProblem || problem || create.isPending) return;
    create.mutate();
  };
  const error = create.isError ? failure(create.error, { 409: t("taken", { slug: shownSlug }) }) : null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (create.isPending) return;
        if (!next) {
          setName("");
          setSlug(null);
          setTried(false);
          create.reset();
        }
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-md" data-testid="system-new-dialog">
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>{t("title")}</DialogTitle>
            <DialogDescription>{t("description", { project })}</DialogDescription>
          </DialogHeader>
          <label className="flex flex-col gap-1.5 text-sm font-medium">
            {t("name")}
            <Input value={name} onChange={(event) => setName(event.target.value)} aria-invalid={(tried && Boolean(nameProblem)) || undefined} autoFocus data-testid="system-new-name" />
            {tried && nameProblem ? <span className="text-xs font-normal text-danger">{nameProblem}</span> : null}
          </label>
          <label className="flex flex-col gap-1.5 text-sm font-medium">
            {t("slug")}
            <Input value={shownSlug} onChange={(event) => setSlug(event.target.value.toLowerCase())} aria-invalid={(tried && Boolean(problem)) || undefined} spellCheck={false} className="font-mono" data-testid="system-new-slug" />
            <span className={tried && problem ? "text-xs font-normal text-danger" : "text-xs font-normal text-muted-foreground"}>
              {tried && problem ? t(`slugProblems.${problem}`) : t("slugHint")}
            </span>
          </label>
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <DialogFooter>
            <Button type="submit" busy={create.isPending} data-testid="system-new-create">
              {create.isPending ? t("creating") : t("create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
