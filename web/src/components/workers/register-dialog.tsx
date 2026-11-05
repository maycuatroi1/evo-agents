"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2, RefreshCw, Server, Terminal, WifiOff, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { InlineError } from "@/components/admin/notice";
import { CommandLine } from "@/components/data/command-line";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import { projectsQuery } from "@/lib/queries";

import { useWorkerFailure, useWorkerWrite } from "./hooks";
import { INSTALL_COMMAND, joinCommand, parseLabels, registerCommand, SERVICE_COMMAND } from "./model";
import { PairingForm, type Write, type Form } from "./pairing-form";
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
} from "./queries";
import { FullPermissions, Steps, Step, CliSteps, Ended, Joined } from "./register-parts";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called once a machine joins with the code, for the page to say so after the dialog closes. */
  onJoined: (notice: Notice) => void;
  /** Where focus goes once the dialog has closed, for a dialog opened without a trigger (the command palette). */
  onCloseAutoFocus?: (event: Event) => void;
};

/**
 * Registering a worker: a pairing code made on the web, which the machine trades for its own worker token, or the
 * CLI alone on a machine already signed in. The code is shown once, counts down to its expiry, and the dialog asks
 * the hub every 2 seconds whether a machine has joined with it. Its content mounts each time it opens, so a new
 * opening starts with an empty form; a code made before stays valid until it expires.
 */
export function RegisterDialog({ open, onOpenChange, onJoined, onCloseAutoFocus }: Props) {
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
        onCloseAutoFocus={onCloseAutoFocus}
        data-testid="register-dialog"
      >
        <RegisterFlow write={write} onClose={() => onOpenChange(false)} onJoined={onJoined} />
      </DialogContent>
    </Dialog>
  );
}

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
  const failure = useWorkerFailure();

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
  const error = write.isError
    ? failure(write.error, { 403: t("errors.forbidden"), 409: t("errors.conflict"), 503: t("errors.unavailable") })
    : null;

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
              alert={error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
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
