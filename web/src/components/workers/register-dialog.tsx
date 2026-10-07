"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleCheck,
  KeyRound,
  Loader2,
  Lock,
  RefreshCw,
  Server,
  Terminal,
  TimerOff,
  TriangleAlert,
  WifiOff,
  X,
} from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { InlineError } from "@/components/admin/notice";
import type { Notice } from "@/components/feedback/toast";
import { useNow } from "@/components/kg/use-now";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import { projectsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { ChipList } from "./badges";
import { CommandLine } from "./command-line";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import {
  INSTALL_COMMAND,
  isWorkerName,
  joinCommand,
  parseLabels,
  readCheckouts,
  readRuntimes,
  registerCommand,
  SERVICE_COMMAND,
} from "./model";
import {
  createPairing,
  MAX_LABELS,
  MAX_SLOTS,
  MIN_SLOTS,
  type Pairing,
  pairingQuery,
  type PairingRequest,
  workerHref,
  workerKeys,
  workerQuery,
} from "./queries";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called once a machine joins with the code, for the page to say so after the dialog closes. */
  onJoined: (notice: Notice) => void;
};

/**
 * Registering a worker: a pairing code made on the web, which the machine trades for its own worker token, or the
 * CLI alone on a machine already signed in. The code is shown once, counts down to its expiry, and the dialog asks
 * the hub every 2 seconds whether a machine has joined with it. Its content mounts each time it opens, so a new
 * opening starts with an empty form; a code made before stays valid until it expires.
 */
export function RegisterDialog({ open, onOpenChange, onJoined }: Props) {
  const write = useWorkerWrite((api, body: PairingRequest) => createPairing(api, body));
  const pending = write.isPending;
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-2xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="register-dialog"
      >
        <RegisterFlow write={write} onClose={() => onOpenChange(false)} onJoined={onJoined} />
      </DialogContent>
    </Dialog>
  );
}

type Write = ReturnType<typeof useWorkerWrite<PairingRequest, Pairing>>;

type Form = { name: string; projects: string[]; slots: string; labels: string; terminal: boolean };
type Errors = Partial<Record<"name" | "projects" | "slots" | "labels", string>>;

function writable(project: Project): boolean {
  return project.role === "writer" || project.role === "admin";
}

function hubUrl(): string {
  return typeof window === "undefined" ? "" : window.location.origin;
}

function RegisterFlow({ write, onClose, onJoined }: { write: Write; onClose: () => void; onJoined: (notice: Notice) => void }) {
  const t = useTranslations("workers.register");
  const projects = useQuery(projectsQuery(browserApi));
  const choices = (projects.data ?? []).filter(writable).map((project) => project.name);
  const [form, setForm] = useState<Form>({ name: "", projects: [], slots: "1", labels: "", terminal: false });
  const [touchedProjects, setTouchedProjects] = useState(false);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [tab, setTab] = useState("pairing");

  // One project to choose from: it is the one, unless the person has changed the choice.
  const chosen = !touchedProjects && form.projects.length === 0 && choices.length === 1 ? choices : form.projects;
  const current = { ...form, projects: chosen };
  const labels = parseLabels(form.labels).labels.slice(0, MAX_LABELS);
  const slots = Number(form.slots);
  const command = registerCommand({
    name: form.name.trim(),
    projects: chosen.length ? chosen : choices.slice(0, 1),
    slots: Number.isInteger(slots) && slots >= MIN_SLOTS && slots <= MAX_SLOTS ? slots : 1,
    labels,
  });

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            <Server className="size-5 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </div>
        <DialogClose asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-lg"
            className="-mt-1 -mr-1 shrink-0"
            aria-label={t("close")}
            disabled={write.isPending}
          >
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </DialogHeader>
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList aria-label={t("tabs.label")}>
          <TabsTrigger value="pairing" data-testid="register-tab-pairing">
            <KeyRound aria-hidden="true" />
            {t("tabs.pairing")}
          </TabsTrigger>
          <TabsTrigger value="cli" data-testid="register-tab-cli">
            <Terminal aria-hidden="true" />
            {t("tabs.cli")}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="pairing">
          {pairing ? (
            <PairingProgress
              pairing={pairing}
              onRestart={() => {
                write.reset();
                setPairing(null);
              }}
              onClose={onClose}
              onJoined={onJoined}
            />
          ) : (
            <PairingForm
              write={write}
              form={current}
              choices={choices}
              loadingProjects={projects.isPending}
              projectsFailed={projects.isError}
              onChange={(next) => {
                if (next.projects !== current.projects) setTouchedProjects(true);
                setForm(next);
              }}
              onCreated={setPairing}
            />
          )}
        </TabsContent>
        <TabsContent value="cli">
          <CliSteps command={command} />
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" size="lg">
                {t("close")}
              </Button>
            </DialogClose>
          </DialogFooter>
        </TabsContent>
      </Tabs>
    </>
  );
}

function FullPermissions({ text }: { text: string }) {
  return (
    <p
      className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention"
      data-testid="register-warning"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span className="text-pretty">{text}</span>
    </p>
  );
}

function FieldError({ id, text }: { id: string; text?: string }) {
  if (!text) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger">
      {text}
    </p>
  );
}

function PairingForm({
  write,
  form,
  choices,
  loadingProjects,
  projectsFailed,
  onChange,
  onCreated,
}: {
  write: Write;
  form: Form;
  choices: string[];
  loadingProjects: boolean;
  projectsFailed: boolean;
  onChange: (form: Form) => void;
  onCreated: (pairing: Pairing) => void;
}) {
  const t = useTranslations("workers.register");
  const failure = useWorkerFailure();
  const ids = useId();
  const [errors, setErrors] = useState<Errors>({});
  const pending = write.isPending;
  const noProjects = !loadingProjects && !projectsFailed && choices.length === 0;

  const describedBy = (field: keyof Errors, hint = true) =>
    [hint ? `${ids}-${field}-hint` : "", errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ") || undefined;
  const set = (patch: Partial<Form>, field?: keyof Errors) => {
    onChange({ ...form, ...patch });
    if (field) setErrors((current) => ({ ...current, [field]: undefined }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const found: Errors = {};
    const name = form.name.trim();
    if (!name) found.name = t("nameRequired");
    else if (!isWorkerName(name)) found.name = t("nameInvalid");
    if (form.projects.length === 0) found.projects = t("projectsRequired");
    const slots = Number(form.slots);
    if (!Number.isInteger(slots) || slots < MIN_SLOTS || slots > MAX_SLOTS) found.slots = t("slotsInvalid", { min: MIN_SLOTS, max: MAX_SLOTS });
    const labels = parseLabels(form.labels);
    if (labels.invalid.length) found.labels = t("labelsInvalid", { labels: labels.invalid.join(", ") });
    else if (labels.tooMany) found.labels = t("labelsTooMany", { max: MAX_LABELS });
    setErrors(found);
    const first = (["name", "projects", "slots", "labels"] as const).find((key) => found[key]);
    if (first) {
      document.getElementById(first === "projects" ? `${ids}-project-0` : `${ids}-${first}`)?.focus();
      return;
    }
    let created: Pairing;
    try {
      created = await write.mutateAsync({
        name,
        projects: form.projects,
        slots,
        labels: labels.labels,
        allow_web_terminal: form.terminal,
      });
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    onCreated(created);
  };

  const error = write.isError
    ? failure(write.error, { 403: t("errors.forbidden"), 409: t("errors.conflict"), 503: t("errors.unavailable") })
    : null;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5" aria-busy={pending || undefined}>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
        <Input
          id={`${ids}-name`}
          name="name"
          value={form.name}
          onChange={(event) => set({ name: event.target.value }, "name")}
          placeholder={t("namePlaceholder")}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={100}
          className="h-9 font-mono placeholder:font-sans"
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={describedBy("name")}
          aria-required="true"
          data-testid="register-name"
        />
        <p id={`${ids}-name-hint`} className="text-xs text-muted-foreground">
          {t("nameHint")}
        </p>
        <FieldError id={`${ids}-name-error`} text={errors.name} />
      </div>

      <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={describedBy("projects")}>
        <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
        {loadingProjects ? (
          <div className="grid gap-2 sm:grid-cols-2" aria-hidden="true">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : projectsFailed ? (
          <p className="text-sm text-danger" role="alert">
            {t("projectsFailed")}
          </p>
        ) : noProjects ? (
          <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="register-no-projects">
            {t("noProjects")}
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2" data-testid="register-projects">
            {choices.map((name, index) => {
              const checked = form.projects.includes(name);
              return (
                <label
                  key={name}
                  htmlFor={`${ids}-project-${index}`}
                  className={cn(
                    "flex min-h-10 cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 transition-colors hover:bg-muted/50",
                    checked && "border-brand/40 bg-surface-selected",
                  )}
                >
                  <input
                    id={`${ids}-project-${index}`}
                    type="checkbox"
                    name="projects"
                    value={name}
                    checked={checked}
                    onChange={(event) =>
                      set(
                        {
                          projects: event.target.checked
                            ? [...form.projects, name]
                            : form.projects.filter((project) => project !== name),
                        },
                        "projects",
                      )
                    }
                    className="size-4 shrink-0 cursor-pointer accent-primary"
                    aria-invalid={errors.projects ? true : undefined}
                  />
                  <span className="min-w-0 font-mono text-sm [overflow-wrap:anywhere]">{name}</span>
                </label>
              );
            })}
          </div>
        )}
        <p id={`${ids}-projects-hint`} className="text-xs text-muted-foreground">
          {t("projectsHint")}
        </p>
        <FieldError id={`${ids}-projects-error`} text={errors.projects} />
      </fieldset>

      <div className="grid gap-5 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-slots`}>{t("slots")}</Label>
          <Input
            id={`${ids}-slots`}
            name="slots"
            type="number"
            inputMode="numeric"
            min={MIN_SLOTS}
            max={MAX_SLOTS}
            step={1}
            value={form.slots}
            onChange={(event) => set({ slots: event.target.value }, "slots")}
            className="h-9 tabular-nums"
            aria-invalid={errors.slots ? true : undefined}
            aria-describedby={describedBy("slots")}
            data-testid="register-slots"
          />
          <p id={`${ids}-slots-hint`} className="text-xs text-muted-foreground">
            {t("slotsHint", { min: MIN_SLOTS, max: MAX_SLOTS })}
          </p>
          <FieldError id={`${ids}-slots-error`} text={errors.slots} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-labels`}>{t("labels")}</Label>
          <Input
            id={`${ids}-labels`}
            name="labels"
            value={form.labels}
            onChange={(event) => set({ labels: event.target.value }, "labels")}
            placeholder={t("labelsPlaceholder")}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={800}
            className="h-9 font-mono placeholder:font-sans"
            aria-invalid={errors.labels ? true : undefined}
            aria-describedby={describedBy("labels")}
            data-testid="register-labels"
          />
          <p id={`${ids}-labels-hint`} className="text-xs text-muted-foreground">
            {t("labelsHint")}
          </p>
          <FieldError id={`${ids}-labels-error`} text={errors.labels} />
        </div>
      </div>

      <label
        htmlFor={`${ids}-terminal`}
        className="flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 transition-colors hover:bg-muted/50"
      >
        <input
          id={`${ids}-terminal`}
          type="checkbox"
          name="allow_web_terminal"
          checked={form.terminal}
          onChange={(event) => set({ terminal: event.target.checked })}
          className="mt-0.5 size-4 shrink-0 cursor-pointer accent-primary"
          aria-describedby={`${ids}-terminal-hint`}
          data-testid="register-terminal"
        />
        <span className="flex flex-col gap-1">
          <span className="text-sm font-medium">{t("terminal")}</span>
          <span id={`${ids}-terminal-hint`} className="text-xs leading-snug text-muted-foreground">
            {t("terminalHint")}
          </span>
        </span>
      </label>

      <FullPermissions text={t("warning")} />
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button
            type="button"
            variant="outline"
            size="lg"
            aria-disabled={pending || undefined}
            className="aria-disabled:opacity-50"
            onClick={(event) => pending && event.preventDefault()}
          >
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" disabled={noProjects} busy={pending} data-testid="register-create">
          <KeyRound aria-hidden="true" />
          {pending ? t("creating") : t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}

function Steps({ children }: { children: ReactNode }) {
  return <ol className="flex flex-col gap-4">{children}</ol>;
}

function Step({ index, title, hint, children }: { index: number; title: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[1.5rem_minmax(0,1fr)] gap-3">
      <span
        className="flex size-6 items-center justify-center rounded-full bg-accent text-xs font-semibold text-accent-foreground tabular-nums"
        aria-hidden="true"
      >
        {index}
      </span>
      <div className="flex min-w-0 flex-col gap-1.5">
        <h3 className="text-sm font-medium">{title}</h3>
        {children}
        {hint ? <p className="text-xs text-pretty text-muted-foreground">{hint}</p> : null}
      </div>
    </li>
  );
}

function CliSteps({ command }: { command: string }) {
  const t = useTranslations("workers.register");
  return (
    <div className="flex flex-col gap-5" data-testid="register-cli">
      <p className="text-sm text-muted-foreground">
        {t.rich("cliIntro", { code: (chunks) => <code className="rounded bg-muted px-1 font-mono text-xs text-foreground">{chunks}</code> })}
      </p>
      <Steps>
        <Step index={1} title={t("stepInstall")}>
          <CommandLine command={INSTALL_COMMAND} label={t("commands.install")} />
        </Step>
        <Step index={2} title={t("cliStepRegister")} hint={t("cliStepRegisterHint")}>
          <CommandLine command={command} label={t("commands.register")} testId="register-cli-command" />
        </Step>
        <Step index={3} title={t("stepService")} hint={t("stepServiceHint")}>
          <CommandLine command={SERVICE_COMMAND} label={t("commands.service")} />
        </Step>
      </Steps>
      <FullPermissions text={t("warning")} />
    </div>
  );
}

const CODE_SECONDS = 10 * 60; // a pairing code lasts 10 minutes (workers.PAIRING_TTL)

/** Whole seconds until the code expires, never more than its life: the hub's clock may run a little ahead. */
function remaining(expiresAt: string, now: number): number {
  return Math.min(CODE_SECONDS, Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000)));
}

function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function PairingProgress({
  pairing,
  onRestart,
  onClose,
  onJoined,
}: {
  pairing: Pairing;
  onRestart: () => void;
  onClose: () => void;
  onJoined: (notice: Notice) => void;
}) {
  const t = useTranslations("workers.register");
  const format = useFormatter();
  const queryClient = useQueryClient();
  const heading = useRef<HTMLHeadingElement>(null);
  const state = useQuery(pairingQuery(browserApi, pairing.id));
  const now = useNow(true);
  const left = now === null ? null : remaining(pairing.expires_at, now);
  const serverStatus = state.data?.status ?? "waiting";
  // The code's own clock: once it runs out the dialog says so at once; the next answer of the hub confirms it.
  const status = serverStatus === "waiting" && left === 0 ? "expired" : serverStatus;
  const workerId = state.data?.worker_id ?? null;

  useEffect(() => heading.current?.focus(), []);
  useEffect(() => {
    if (status !== "joined") return;
    void queryClient.invalidateQueries({ queryKey: workerKeys.list });
    onJoined({
      tone: "success",
      text: t("joinedNotice", { name: pairing.name }),
      link: workerId !== null ? { label: t("openWorker"), href: workerHref(workerId) } : null,
    });
    // Once per pairing: the toast is the page's record that the machine joined.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  return (
    <div className="flex flex-col gap-5" data-testid="pairing-progress" data-status={status}>
      <h3 ref={heading} tabIndex={-1} className="sr-only">
        {t("codeHeading", { name: pairing.name })}
      </h3>
      {/* Every change of state is announced: waiting, connected, expired, locked. */}
      <div role="status" aria-live="polite">
        {status === "joined" ? (
          <Joined name={pairing.name} usedAt={state.data?.used_at ?? null} workerId={workerId} />
        ) : status === "expired" || status === "locked" ? (
          <Ended status={status} />
        ) : (
          <p className="flex items-center gap-2 rounded-md border bg-accent px-3 py-2.5 text-sm text-accent-foreground" data-testid="pairing-waiting">
            <Loader2 className="size-4 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            {t("waiting")}
          </p>
        )}
      </div>

      {status === "waiting" ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 rounded-md border bg-card shadow-raised px-4 py-3">
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-xs text-muted-foreground">{t("code")}</span>
              <span
                className="font-mono text-2xl font-medium tracking-[0.14em] tabular-nums sm:text-3xl"
                data-testid="pairing-code"
              >
                {pairing.code}
              </span>
            </div>
            <div className="flex flex-col items-start gap-1 sm:items-end">
              <span role="timer" className="text-sm text-muted-foreground tabular-nums" data-testid="pairing-expires">
                {left === null
                  ? t("expiresAt", { time: format.dateTime(new Date(pairing.expires_at), { timeStyle: "short" }) })
                  : t("expiresIn", { time: clock(left) })}
              </span>
              {state.data && state.data.tries_left < 5 ? (
                <span className="text-xs text-attention" data-testid="pairing-tries">
                  {t("triesLeft", { count: state.data.tries_left })}
                </span>
              ) : null}
            </div>
          </div>
          <Steps>
            <Step index={1} title={t("stepInstall")}>
              <CommandLine command={INSTALL_COMMAND} label={t("commands.install")} />
            </Step>
            <Step index={2} title={t("stepJoin")} hint={t("stepJoinHint")}>
              <CommandLine command={joinCommand(hubUrl(), pairing.code)} label={t("commands.join")} testId="pairing-join-command" />
            </Step>
            <Step index={3} title={t("stepService")} hint={t("stepServiceHint")}>
              <CommandLine command={SERVICE_COMMAND} label={t("commands.service")} />
            </Step>
          </Steps>
          <FullPermissions text={t("warning")} />
          {state.isError ? (
            <p className="flex items-start gap-2 text-sm text-muted-foreground" data-testid="pairing-poll-error">
              <WifiOff className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              {t("pollFailed")}
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            {t("closeHint", { time: format.dateTime(new Date(pairing.expires_at), { timeStyle: "short" }) })}
          </p>
        </>
      ) : null}

      <DialogFooter>
        {status === "expired" || status === "locked" ? (
          <Button type="button" variant="outline" size="lg" onClick={onRestart} data-testid="pairing-restart">
            <RefreshCw aria-hidden="true" />
            {t("newCode")}
          </Button>
        ) : null}
        <Button type="button" variant={status === "joined" ? "outline" : "default"} size="lg" onClick={onClose}>
          {t("close")}
        </Button>
        {status === "joined" && workerId !== null ? (
          <Button asChild size="lg">
            <Link href={workerHref(workerId)} onClick={onClose} data-testid="pairing-open-worker">
              <Server aria-hidden="true" />
              {t("openWorker")}
            </Link>
          </Button>
        ) : null}
      </DialogFooter>
    </div>
  );
}

function Ended({ status }: { status: "expired" | "locked" }) {
  const t = useTranslations("workers.register");
  const Icon = status === "locked" ? Lock : TimerOff;
  return (
    <div className="flex items-start gap-3 rounded-md border border-danger/30 bg-danger-soft px-3 py-3 text-sm" data-testid="pairing-ended">
      <Icon className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium text-danger">{status === "locked" ? t("lockedTitle") : t("expiredTitle")}</p>
        <p className="text-muted-foreground">{status === "locked" ? t("lockedBody") : t("expiredBody")}</p>
      </div>
    </div>
  );
}

function Joined({ name, usedAt, workerId }: { name: string; usedAt: string | null; workerId: number | null }) {
  const t = useTranslations("workers.register");
  const tDetail = useTranslations("workers.detail");
  const format = useFormatter();
  const worker = useQuery({ ...workerQuery(browserApi, workerId ?? 0), enabled: workerId !== null });
  const data = worker.data;
  const runtimes = data ? readRuntimes(data.runtimes).filter((runtime) => runtime.available !== false) : [];
  const checkouts = data ? readCheckouts(data.checkouts) : [];
  return (
    <div className="flex flex-col gap-4" data-testid="pairing-joined">
      <p className="flex items-start gap-2.5 rounded-md border border-success/20 bg-success-soft px-3 py-2.5 text-sm text-success">
        <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span>
          {t.rich("joinedTitle", {
            name,
            time: usedAt ? format.dateTime(new Date(usedAt), { timeStyle: "medium" }) : "",
            strong: (chunks) => <strong className="font-mono font-medium">{chunks}</strong>,
          })}
        </span>
      </p>
      {data ? (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-sm" data-testid="pairing-joined-facts">
          <dt className="text-muted-foreground">{tDetail("scope.host")}</dt>
          <dd className="font-mono text-xs [overflow-wrap:anywhere]">
            {t("hostValue", { hostname: data.hostname, os: data.os, arch: data.arch })}
          </dd>
          <dt className="text-muted-foreground">{tDetail("runtimes.title")}</dt>
          <dd>
            <ChipList items={runtimes.map((runtime) => runtime.name)} empty={t("firstHeartbeat")} />
          </dd>
          <dt className="text-muted-foreground">{tDetail("checkouts.title")}</dt>
          <dd>
            <ChipList items={checkouts.map((checkout) => checkout.name)} empty={t("firstHeartbeat")} />
          </dd>
          <dt className="text-muted-foreground">{tDetail("scope.projects")}</dt>
          <dd>
            <ChipList items={data.projects} empty="-" />
          </dd>
          <dt className="text-muted-foreground">{tDetail("scope.slots")}</dt>
          <dd className="tabular-nums">{data.slots}</dd>
        </dl>
      ) : worker.isPending && workerId !== null ? (
        <Skeleton className="h-24 w-full" />
      ) : null}
    </div>
  );
}
