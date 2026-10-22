"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type ReactNode, type RefObject, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { CheckList, CheckListSkeleton, EmptyPick, FieldError, Hint } from "./login-fields";
import { offeredWorkers } from "./agent-model";
import { workersQuery } from "./login-queries";

/** The fields of the dialogs of an agent's accounts: a line of text, the value, the workers and the end. */

type Field = { id: string; label: string; hint: ReactNode; problem: string | null; testId: string };

function described(id: string, problem: string | null) {
  return problem ? `${id}-hint ${id}-error` : `${id}-hint`;
}

export function TextField({
  id,
  label,
  hint,
  problem,
  testId,
  value,
  onChange,
  maxLength,
}: Field & { value: string; onChange: (value: string) => void; maxLength: number }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        maxLength={maxLength}
        autoComplete="off"
        spellCheck={false}
        className="h-9 font-mono text-[13px]"
        aria-invalid={problem ? true : undefined}
        aria-describedby={described(id, problem)}
        aria-required="true"
        data-testid={testId}
      />
      <Hint id={`${id}-hint`}>{hint}</Hint>
      <FieldError id={`${id}-error`} text={problem} testId={`${testId}-error`} />
    </div>
  );
}

/**
 * The value: a password field the page never fills. It holds no `name`, so no browser offers to keep it, and the
 * dialog reads it once, when it is sent; closing the dialog drops it.
 */
export function ValueField({ id, label, hint, problem, testId, inputRef }: Field & { inputRef: RefObject<HTMLInputElement | null> }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        ref={inputRef}
        id={id}
        type="password"
        autoComplete="off"
        spellCheck={false}
        className="h-9 font-mono text-[13px]"
        aria-invalid={problem ? true : undefined}
        aria-describedby={described(id, problem)}
        aria-required="true"
        data-testid={testId}
      />
      <Hint id={`${id}-hint`}>{hint}</Hint>
      <FieldError id={`${id}-error`} text={problem} testId={`${testId}-error`} />
    </div>
  );
}

/** The day the account ends, or none: the hub stops leasing it from 00:00 UTC of that day. */
export function EndField({ id, label, hint, problem, testId, value, onChange }: Field & { value: string; onChange: (value: string) => void }) {
  const [minDay] = useState(() => new Date(Date.now() + 86_400_000).toISOString().slice(0, 10));
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="date"
        min={minDay}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 w-full tabular-nums sm:w-48"
        aria-invalid={problem ? true : undefined}
        aria-describedby={described(id, problem)}
        data-testid={testId}
      />
      <Hint id={`${id}-hint`}>{hint}</Hint>
      <FieldError id={`${id}-error`} text={problem} testId={`${testId}-error`} />
    </div>
  );
}

/**
 * The workers whose runs under the agent take the account: the admin's own and the shared ones that serve the
 * project, and those it is bound to already.
 */
export function WorkersField({
  id,
  project,
  picked,
  current,
  problem,
  onChange,
}: {
  id: string;
  project: string;
  picked: string[];
  current: readonly string[];
  problem: string | null;
  onChange: (workers: string[]) => void;
}) {
  const t = useTranslations("accounts.agent.form");
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const workers = useQuery(workersQuery(browserApi));
  const names = offeredWorkers(workers.data ?? [], me?.login, project, current);
  const toggle = (name: string, on: boolean) => onChange(on ? [...picked, name] : picked.filter((worker) => worker !== name));
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={described(id, problem)}>
      <legend className="mb-1.5 text-sm font-medium">{t("workers")}</legend>
      {workers.isPending ? (
        <CheckListSkeleton />
      ) : workers.isError ? (
        <p className="text-sm text-danger" role="alert">
          {t("workersFailed")}
        </p>
      ) : names.length === 0 ? (
        <EmptyPick testId="agent-account-no-workers">{t("noWorkers", { project })}</EmptyPick>
      ) : (
        <CheckList idPrefix={`${id}-worker`} names={names} picked={picked} onToggle={toggle} invalid={Boolean(problem)} testId="agent-account-workers" />
      )}
      <Hint id={`${id}-hint`}>{t("workersHint", { project })}</Hint>
      <FieldError id={`${id}-error`} text={problem} testId="agent-account-workers-error" />
    </fieldset>
  );
}
