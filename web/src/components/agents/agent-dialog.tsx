"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot, Info, Trash2, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useMemo, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { InlineError } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { type Agent, AXES } from "@/lib/agents";
import { MAX_DESCRIPTION_CHARS, MAX_INSTRUCTIONS_CHARS, MAX_NAME_CHARS } from "@/lib/agent-rules";
import { browserApi } from "@/lib/api/browser";

import { AxisField } from "./axis-field";
import { AvatarPicker, ColourField, Field } from "./form-parts";
import { useAgentFailure, useAvatarWrite, useCreateAgent, useEditAgent } from "./hooks";
import { type AgentDraft, createBody, draftOf, draftProblems, editBody, emptyDraft, fileBase64, type FieldProblem, hasProblems } from "./model";
import { agentOptionsQuery } from "./options";

type Props = {
  project: string;
  /** The agent to change; null to create one. */
  agent: Agent | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the agent as the hub saved it, once the dialog closes. */
  onSaved?: (agent: Agent) => void;
};

/**
 * New agent and Edit agent, for an admin of the project (web/DESIGN.md, Agents): who the agent is (handle, creating
 * only, since it never changes; name, description, colour, and creating, an avatar) and, for any agent but the
 * project's default, its instructions and its seven axes. The form repeats the hub's checks and shows them once the
 * admin tries to save; an edit sends only what changed, against the version it opened on. The content mounts each
 * time the dialog opens, so it starts from the agent as the hub holds it.
 */
export function AgentDialog({ project, agent, open, onOpenChange, onSaved }: Props) {
  const [pending, setPending] = useState(false);
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="agent-dialog"
        data-mode={agent ? "edit" : "create"}
      >
        {open ? (
          <AgentForm project={project} agent={agent} onPending={setPending} onClose={() => onOpenChange(false)} onSaved={onSaved} />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function useFieldText() {
  const t = useTranslations("agents.form.problems");
  return (problem: FieldProblem | undefined, max?: number): string | null =>
    problem ? t(problem, { max: max ?? 0 }) : null;
}

function AgentForm({
  project,
  agent,
  onPending,
  onClose,
  onSaved,
}: {
  project: string;
  agent: Agent | null;
  onPending: (pending: boolean) => void;
  onClose: () => void;
  onSaved?: (agent: Agent) => void;
}) {
  const t = useTranslations("agents.form");
  const format = useFormatter();
  const failure = useAgentFailure();
  const fieldText = useFieldText();
  const options = useQuery(agentOptionsQuery(browserApi, project));
  const create = useCreateAgent(project);
  const edit = useEditAgent(project, agent?.handle ?? "");
  const avatar = useAvatarWrite(project);
  const [draft, setDraft] = useState<AgentDraft>(() => (agent ? draftOf(agent) : emptyDraft()));
  const [file, setFile] = useState<File | null>(null);
  const [tried, setTried] = useState(false);
  const write = agent ? edit : create;
  const pending = write.isPending || avatar.isPending;
  const creating = agent === null;
  const restricted = !agent?.is_default;

  useEffect(() => onPending(pending), [pending, onPending]);
  // The picked image, shown before it is sent; its object URL is let go once another is picked or the form closes.
  const preview = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);
  useEffect(() => () => (preview ? URL.revokeObjectURL(preview) : undefined), [preview]);

  const projects = [project, ...(options.data?.byAxis.project.map((option) => option.value) ?? [])];
  const problems = draftProblems(draft, { creating, projects });
  const shown = tried ? problems : { axes: {} };
  const changes = agent ? editBody(draft, agent) : null;
  const set = (patch: Partial<AgentDraft>) => {
    write.reset();
    setDraft((current) => ({ ...current, ...patch }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setTried(true);
    if (hasProblems(problems) || (agent && !changes)) return;
    let saved: Agent;
    try {
      saved = agent ? await edit.mutateAsync(changes ?? {}) : await create.mutateAsync(createBody(draft));
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    if (file) {
      try {
        saved = await avatar.mutateAsync({ handle: saved.handle, data: await fileBase64(file) });
      } catch (error) {
        notifyFailure(t("avatarFailed", { handle: saved.handle }), failure(error));
      }
    }
    notify({
      tone: "success",
      text: creating ? t("created", { handle: saved.handle }) : t("saved", { handle: saved.handle, version: saved.version }),
      description: creating ? t("createdText") : t("savedText"),
    });
    onClose();
    onSaved?.(saved);
  };

  const error = write.isError
    ? failure(write.error, { 409: creating ? t("errors.taken") : t("errors.moved", { handle: agent?.handle ?? "" }) })
    : null;
  const count = (text: string, max: number) => t("chars", { count: format.number([...text].length), max: format.number(max) });

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex min-h-0 flex-1 flex-col" aria-busy={pending || undefined}>
      <div className="flex items-start gap-3 border-b px-4 pt-4 pb-3 sm:px-5">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            <Bot className="size-5 text-muted-foreground" aria-hidden="true" />
            {creating ? t("newTitle") : t("editTitle", { handle: agent.handle })}
          </DialogTitle>
          <DialogDescription className="text-pretty">{creating ? t("newDescription", { project }) : t("editDescription", { version: agent.version })}</DialogDescription>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={t("close")} disabled={pending}>
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4 sm:px-5" data-testid="agent-form-body">
        <div className="grid gap-4 sm:grid-cols-2">
          {creating ? (
            <Field label={t("handle")} hint={t("handleHint")} error={fieldText(shown.handle, 100)} testId="agent-handle">
              {({ id, describedBy, invalid }) => (
                <div className="flex min-w-0 items-center rounded-sm border border-input bg-card focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50">
                  <span className="pl-2.5 font-mono text-sm text-muted-foreground" aria-hidden="true">
                    @
                  </span>
                  <Input
                    id={id}
                    value={draft.handle}
                    onChange={(event) => set({ handle: event.target.value.toLowerCase() })}
                    autoComplete="off"
                    spellCheck={false}
                    aria-invalid={invalid || undefined}
                    aria-describedby={describedBy || undefined}
                    className="border-0 pl-1 font-mono shadow-none focus-visible:ring-0"
                    data-testid="agent-handle-input"
                  />
                </div>
              )}
            </Field>
          ) : null}
          <Field label={t("name")} hint={t("nameHint")} error={fieldText(shown.name, MAX_NAME_CHARS)} testId="agent-name">
            {({ id, describedBy, invalid }) => (
              <Input
                id={id}
                value={draft.name}
                onChange={(event) => set({ name: event.target.value })}
                aria-invalid={invalid || undefined}
                aria-describedby={describedBy || undefined}
                data-testid="agent-name-input"
              />
            )}
          </Field>
        </div>

        <Field
          label={t("description")}
          optional={t("optional")}
          hint={`${t("descriptionHint")} ${count(draft.description, MAX_DESCRIPTION_CHARS)}`}
          error={fieldText(shown.description, MAX_DESCRIPTION_CHARS)}
          testId="agent-description"
        >
          {({ id, describedBy, invalid }) => (
            <Textarea
              id={id}
              value={draft.description}
              rows={2}
              onChange={(event) => set({ description: event.target.value })}
              aria-invalid={invalid || undefined}
              aria-describedby={describedBy || undefined}
              className="max-h-40"
              data-testid="agent-description-input"
            />
          )}
        </Field>

        <ColourField value={draft.color} onChange={(color) => set({ color })} allowAuto={creating} disabled={pending} />

        {creating ? (
          <div className="flex min-w-0 flex-col gap-2" data-testid="agent-avatar-field">
            <span className="text-sm font-medium">
              {t("avatar.label")} <span className="font-normal text-muted-foreground">{t("optional")}</span>
            </span>
            <div className="flex flex-wrap items-center gap-4">
              <AgentAvatar agent={{ name: draft.name || draft.handle || "?", handle: draft.handle || "?", color: draft.color ?? "#475569", icon_url: null }} size="xl" src={preview} />
              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap gap-2">
                  <AvatarPicker label={file ? t("avatar.replace") : t("avatar.choose")} onPick={setFile} disabled={pending} />
                  {file ? (
                    <Button type="button" variant="ghost" onClick={() => setFile(null)} disabled={pending} data-testid="agent-avatar-clear">
                      <Trash2 aria-hidden="true" />
                      {t("avatar.clear")}
                    </Button>
                  ) : null}
                </div>
                <p className="text-xs text-pretty text-muted-foreground">{file ? t("avatar.picked", { name: file.name }) : t("avatar.hint")}</p>
              </div>
            </div>
          </div>
        ) : null}

        {restricted ? (
          <>
            <Field
              label={t("instructions")}
              optional={t("optional")}
              hint={`${t("instructionsHint")} ${count(draft.instructions, MAX_INSTRUCTIONS_CHARS)}`}
              error={fieldText(shown.instructions, MAX_INSTRUCTIONS_CHARS)}
              testId="agent-instructions"
            >
              {({ id, describedBy, invalid }) => (
                <Textarea
                  id={id}
                  value={draft.instructions}
                  rows={5}
                  onChange={(event) => set({ instructions: event.target.value })}
                  aria-invalid={invalid || undefined}
                  aria-describedby={describedBy || undefined}
                  className="max-h-[40dvh] font-mono text-[13px]"
                  data-testid="agent-instructions-input"
                />
              )}
            </Field>
            <section className="flex min-w-0 flex-col gap-3" aria-labelledby="agent-permissions-title" data-testid="agent-permissions-form">
              <div className="flex flex-col gap-1">
                <h3 id="agent-permissions-title" className="text-sm font-medium">
                  {t("permissions")}
                </h3>
                <p className="text-xs text-pretty text-muted-foreground">{t("permissionsHint")}</p>
              </div>
              {AXES.map((axis) => (
                <AxisField
                  key={axis}
                  axis={axis}
                  value={draft.permissions[axis]}
                  onChange={(rule) => set({ permissions: { ...draft.permissions, [axis]: rule } })}
                  options={options.data?.byAxis[axis] ?? []}
                  loading={options.isPending}
                  failed={options.isError || (options.data?.failed.includes(axis) ?? false)}
                  problem={shown.axes[axis]}
                  disabled={pending}
                />
              ))}
            </section>
          </>
        ) : (
          <p role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-pretty text-muted-foreground" data-testid="agent-default-note">
            <Info className="mt-px size-4 shrink-0" aria-hidden="true" />
            <span>{t("defaultNote", { project })}</span>
          </p>
        )}

        {tried && hasProblems(problems) ? (
          <p className="text-sm font-medium text-danger" role="alert" data-testid="agent-form-problems">
            {t("fixFirst")}
          </p>
        ) : null}
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      </div>

      <div className="flex flex-col-reverse gap-2 border-t bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:justify-end sm:px-5">
        {agent && !changes ? (
          <p className="mr-auto text-xs text-muted-foreground" aria-live="polite" data-testid="agent-form-unchanged">
            {t("unchanged")}
          </p>
        ) : null}
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg" aria-disabled={pending || undefined} className="aria-disabled:opacity-50" onClick={(event) => pending && event.preventDefault()}>
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={pending} disabled={Boolean(agent) && !changes} data-testid="agent-save">
          {pending ? t("saving") : creating ? t("create") : t("save")}
        </Button>
      </div>
    </form>
  );
}
