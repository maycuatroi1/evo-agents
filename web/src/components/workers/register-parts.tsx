"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, Lock, TimerOff, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode } from "react";

import { CommandLine } from "@/components/data/command-line";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";

import { ChipList } from "./badges";
import { INSTALL_COMMAND, readCheckouts, readRuntimes, SERVICE_COMMAND } from "./model";
import { workerQuery } from "./queries";

export function FullPermissions({ text }: { text: string }) {
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

export function Steps({ children }: { children: ReactNode }) {
  return <ol className="flex flex-col gap-4">{children}</ol>;
}

export function Step({ index, title, hint, children }: { index: number; title: string; hint?: ReactNode; children: ReactNode }) {
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

export function CliSteps({ command }: { command: string }) {
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

export function Ended({ status }: { status: "expired" | "locked" }) {
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

export function Joined({ name, usedAt, workerId }: { name: string; usedAt: string | null; workerId: number | null }) {
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
