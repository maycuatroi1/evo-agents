"use client";

import { ArrowRight, Info } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useState } from "react";

import { CommandLine } from "@/components/data/command-line";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PROJECT_NAME } from "@/lib/queries";

import {
  CLAUDE_PLUGIN_COMMANDS,
  CODEX_PLUGIN_COMMANDS,
  harnessInitCommand,
  hubLoginCommand,
  INSTALL_COMMAND,
  REGISTER_COMMAND,
  SERVICE_COMMAND,
} from "./commands";
import type { InvitePreview } from "./queries";

type Props = {
  invite: Pick<InvitePreview, "kind" | "project" | "role">;
  hubUrl: string;
  /** The button that makes a pairing code, from the workers feature (the page composes the two). */
  pairing: ReactNode;
};

function Step({ number, title, hint, children, testId }: { number: number; title: string; hint?: ReactNode; children?: ReactNode; testId: string }) {
  return (
    <li className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 sm:gap-x-4" data-testid={testId}>
      <span
        className="grid size-7 place-items-center rounded-full border bg-card text-[13px] font-semibold tabular-nums text-muted-foreground"
        aria-hidden="true"
      >
        {number}
      </span>
      <div className="flex min-w-0 flex-col gap-2.5 pb-1">
        <h3 className="pt-0.5 text-[15px] leading-6 font-semibold text-balance">{title}</h3>
        {hint ? <p className="text-sm text-pretty text-muted-foreground">{hint}</p> : null}
        {children}
      </div>
    </li>
  );
}

/** A link button whose label carries a project name: it wraps, anywhere in a long name, instead of widening the page. */
const NAMED_LINK = "h-auto min-h-8 max-w-full self-start py-1.5 text-left whitespace-normal [overflow-wrap:anywhere]";

function AgentCommands({ agent, commands, testId }: { agent: string; commands: readonly string[]; testId: string }) {
  const t = useTranslations("invite.start.plugins");
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <p className="text-xs font-medium text-muted-foreground">{agent}</p>
      {commands.map((command, index) => (
        <CommandLine
          key={command}
          command={command}
          label={t("label", { agent, number: index + 1 })}
          testId={`${testId}-${index + 1}`}
        />
      ))}
    </div>
  );
}

/**
 * What to run after accepting a link, in order: the CLI, its sign-in to this hub, the plugins, a harness and its
 * registration (a register link only), a worker paired with a code and kept running as a service, then Run plan.
 * A reader cannot pair a worker or run a plan, so their list ends with the project to read.
 */
export function GetStarted({ invite, hubUrl, pairing }: Props) {
  const t = useTranslations("invite.start");
  const ids = useId();
  const [name, setName] = useState("");
  const register = invite.kind === "register";
  const reader = invite.kind === "join" && invite.role === "reader";
  const project = invite.project;
  const typed = name.trim();
  const invalid = typed !== "" && !PROJECT_NAME.test(typed);
  const next = register ? 5 : 4; // the step after the plugins, and after the harness of a register link

  return (
    <section aria-labelledby={`${ids}-title`} className="flex flex-col gap-5" data-testid="invite-get-started">
      <div className="flex flex-col gap-1">
        <h2 id={`${ids}-title`} className="text-lg font-semibold tracking-tight">
          {t("title")}
        </h2>
        <p className="text-sm text-pretty text-muted-foreground">{t("description")}</p>
      </div>
      <ol className="flex flex-col gap-6">
        <Step number={1} title={t("install.title")} hint={t("install.hint")} testId="invite-step-install">
          <CommandLine command={INSTALL_COMMAND} label={t("install.label")} testId="invite-command-install" />
        </Step>
        <Step number={2} title={t("login.title")} hint={t("login.hint")} testId="invite-step-login">
          <CommandLine command={hubLoginCommand(hubUrl)} label={t("login.label")} testId="invite-command-login" />
        </Step>
        <Step number={3} title={t("plugins.title")} hint={t("plugins.hint")} testId="invite-step-plugins">
          <AgentCommands agent={t("plugins.claude")} commands={CLAUDE_PLUGIN_COMMANDS} testId="invite-command-claude" />
          <AgentCommands agent={t("plugins.codex")} commands={CODEX_PLUGIN_COMMANDS} testId="invite-command-codex" />
        </Step>
        {register ? (
          <Step number={4} title={t("project.title")} hint={t("project.hint")} testId="invite-step-project">
            <div className="flex max-w-sm flex-col gap-1.5">
              <Label htmlFor={`${ids}-name`}>{t("project.name")}</Label>
              <Input
                id={`${ids}-name`}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="acme-tools"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={100}
                className="h-9 font-mono"
                aria-invalid={invalid || undefined}
                aria-describedby={`${ids}-name-hint${invalid ? ` ${ids}-name-error` : ""}`}
                data-testid="invite-project-name"
              />
              <p id={`${ids}-name-hint`} className="text-xs text-muted-foreground">
                {t("project.nameHint")}
              </p>
              {invalid ? (
                <p id={`${ids}-name-error`} className="text-xs font-medium text-danger">
                  {t("project.invalid")}
                </p>
              ) : null}
            </div>
            <CommandLine command={harnessInitCommand(name)} label={t("project.initLabel")} testId="invite-command-harness" />
            <CommandLine command={REGISTER_COMMAND} label={t("project.registerLabel")} testId="invite-command-register" />
          </Step>
        ) : null}
        {reader ? (
          <Step number={next} title={t("read.title")} hint={t("read.hint")} testId="invite-step-read">
            {project ? (
              <Button asChild variant="outline" className={NAMED_LINK}>
                <Link href={`/p/${encodeURIComponent(project)}` as Route} data-testid="invite-open-project">
                  {t("read.open", { project })}
                  <ArrowRight aria-hidden="true" />
                </Link>
              </Button>
            ) : null}
          </Step>
        ) : (
          <>
            <Step number={next} title={t("worker.title")} hint={t("worker.hint")} testId="invite-step-worker">
              {register && !project ? <p className="text-sm text-pretty text-muted-foreground">{t("worker.afterRegister")}</p> : null}
              <div className="self-start">{pairing}</div>
              <CommandLine command={SERVICE_COMMAND} label={t("worker.serviceLabel")} testId="invite-command-service" />
              <p className="flex items-start gap-2 rounded-md border bg-accent px-3 py-2.5 text-sm text-accent-foreground" data-testid="invite-credentials">
                <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                {t("worker.credentials")}
              </p>
            </Step>
            <Step number={next + 1} title={t("run.title")} hint={project ? t("run.hint") : t("run.noProject")} testId="invite-step-run">
              <Button asChild variant="outline" className={NAMED_LINK}>
                {project ? (
                  <Link href={`/p/${encodeURIComponent(project)}/plans` as Route} data-testid="invite-run-plans">
                    {t("run.open", { project })}
                    <ArrowRight aria-hidden="true" />
                  </Link>
                ) : (
                  <Link href="/" data-testid="invite-open-home">
                    {t("run.home")}
                    <ArrowRight aria-hidden="true" />
                  </Link>
                )}
              </Button>
            </Step>
          </>
        )}
      </ol>
    </section>
  );
}
