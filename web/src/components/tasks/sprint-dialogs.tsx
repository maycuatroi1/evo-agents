"use client";

import { CalendarPlus, CircleCheckBig } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { createSprint, editSprint, type Sprint } from "./api";
import { signInAgainOn401, useTasksRefresh } from "./hooks";
import { orderedSprints } from "./model";

const MAX_NAME = 100; // evo_agents.hub.tables_tasks.MAX_SPRINT_NAME_CHARS
const MAX_GOAL = 2_000; // MAX_GOAL_CHARS

/** A dialog that stays open while its write runs, and shows a refusal inside. */
function useDialogWrite(onClose: () => void) {
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const run = async (write: () => Promise<string>) => {
    setBusy(true);
    setRefused(null);
    try {
      notify({ tone: "success", text: await write() });
      onClose();
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
    } finally {
      setBusy(false);
    }
  };
  return { busy, refused, run };
}

function NewSprintForm({ project, canStart, onClose }: { project: string; canStart: boolean; onClose: () => void }) {
  const t = useTranslations("tasks.sprintDialog");
  const ids = useId();
  const refresh = useTasksRefresh(project);
  const { busy, refused, run } = useDialogWrite(onClose);
  const [form, setForm] = useState({ name: "", goal: "", starts_on: "", ends_on: "", start: false });
  const [missing, setMissing] = useState(false);
  const backwards = form.starts_on !== "" && form.ends_on !== "" && form.ends_on < form.starts_on;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const name = form.name.trim();
    if (!name) {
      setMissing(true);
      document.getElementById(`${ids}-name`)?.focus();
      return;
    }
    if (backwards || busy) return;
    void run(async () => {
      await createSprint(browserApi(), project, {
        name,
        goal: form.goal.trim(),
        starts_on: form.starts_on || null,
        ends_on: form.ends_on || null,
        state: form.start && canStart ? "active" : "planned",
      });
      await refresh();
      return form.start && canStart ? t("started", { sprint: name }) : t("created", { sprint: name });
    });
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5" data-testid="sprint-new-form">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <CalendarPlus className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("newTitle")}
        </DialogTitle>
        <DialogDescription>{t("newText")}</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
        <Input
          id={`${ids}-name`}
          value={form.name}
          onChange={(event) => {
            setForm((held) => ({ ...held, name: event.target.value }));
            setMissing(false);
          }}
          maxLength={MAX_NAME}
          placeholder={t("namePlaceholder")}
          aria-invalid={missing || undefined}
          aria-describedby={missing ? `${ids}-name-error` : undefined}
          aria-required="true"
          data-testid="sprint-name"
        />
        {missing ? (
          <p id={`${ids}-name-error`} className="text-xs font-medium text-danger">
            {t("nameMissing")}
          </p>
        ) : null}
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-goal`}>{t("goal")}</Label>
        <Textarea id={`${ids}-goal`} value={form.goal} onChange={(event) => setForm((held) => ({ ...held, goal: event.target.value }))} rows={2} maxLength={MAX_GOAL} />
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-starts`}>{t("starts")}</Label>
          <Input id={`${ids}-starts`} type="date" value={form.starts_on} onChange={(event) => setForm((held) => ({ ...held, starts_on: event.target.value }))} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-ends`}>{t("ends")}</Label>
          <Input
            id={`${ids}-ends`}
            type="date"
            value={form.ends_on}
            onChange={(event) => setForm((held) => ({ ...held, ends_on: event.target.value }))}
            aria-invalid={backwards || undefined}
            aria-describedby={backwards ? `${ids}-ends-error` : undefined}
          />
          {backwards ? (
            <p id={`${ids}-ends-error`} className="text-xs font-medium text-danger">
              {t("backwards")}
            </p>
          ) : null}
        </div>
      </div>
      {canStart ? (
        <div className="flex items-center gap-2">
          <Switch id={`${ids}-start`} checked={form.start} onCheckedChange={(start) => setForm((held) => ({ ...held, start }))} />
          <Label htmlFor={`${ids}-start`} className="font-normal">
            {t("startNow")}
          </Label>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">{t("oneActive")}</p>
      )}
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="sprint-create">
          {t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}

function CloseSprintForm({ project, sprint, sprints, onClose }: { project: string; sprint: Sprint; sprints: readonly Sprint[]; onClose: () => void }) {
  const t = useTranslations("tasks.sprintDialog");
  const id = useId();
  const refresh = useTasksRefresh(project);
  const { busy, refused, run } = useDialogWrite(onClose);
  const targets = orderedSprints(sprints).filter((one) => one.state === "planned");
  const [moveTo, setMoveTo] = useState(targets[0]?.name ?? "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    void run(async () => {
      await editSprint(browserApi(), project, sprint.name, { state: "closed", move_to: moveTo || null });
      await refresh();
      return t("closed", { sprint: sprint.name });
    });
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-5" data-testid="sprint-close-form">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <CircleCheckBig className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("closeTitle", { sprint: sprint.name })}
        </DialogTitle>
        <DialogDescription>{t("closeText")}</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={id}>{t("moveTo")}</Label>
        <NativeSelect id={id} value={moveTo} onChange={(event) => setMoveTo(event.target.value)} className="w-full" data-testid="sprint-move-to">
          {targets.map((one) => (
            <NativeSelectOption key={one.name} value={one.name}>
              {one.name}
            </NativeSelectOption>
          ))}
          <NativeSelectOption value="">{t("noSprint")}</NativeSelectOption>
        </NativeSelect>
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="sprint-close-confirm">
          {t("close")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** What the backlog's sprint dialog does: start a new sprint, or close the active one. */
export type SprintDialogFor = { kind: "new" } | { kind: "close"; sprint: Sprint } | null;

/**
 * The backlog's sprint dialog: a new sprint (name, goal, days, started at once while none is active), or closing the
 * active one, which moves its unfinished tasks to a planned sprint or out of every sprint. Esc and clicks outside do
 * nothing while the write runs; a refusal shows inside.
 */
export function SprintDialog({ project, sprints, open, onOpenChange }: { project: string; sprints: readonly Sprint[]; open: SprintDialogFor; onOpenChange: (open: SprintDialogFor) => void }) {
  const close = () => onOpenChange(null);
  const canStart = !sprints.some((sprint) => sprint.state === "active");
  return (
    <Dialog open={open !== null} onOpenChange={(next) => (next ? undefined : close())}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg" data-testid="sprint-dialog">
        {open?.kind === "new" ? <NewSprintForm project={project} canStart={canStart} onClose={close} /> : null}
        {open?.kind === "close" ? <CloseSprintForm project={project} sprint={open.sprint} sprints={sprints} onClose={close} /> : null}
      </DialogContent>
    </Dialog>
  );
}
