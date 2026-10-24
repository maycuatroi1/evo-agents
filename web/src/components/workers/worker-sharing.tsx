"use client";

import { useQuery } from "@tanstack/react-query";
import { Building2, Plus, Stethoscope, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { shareChartQuery } from "@/components/sharing/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { useNow } from "@/hooks/use-now";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { Ago } from "./ago";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import type { Worker } from "./queries";
import { SectionCard } from "./section-card";
import { SharedSwitch } from "./shared-switch";
import { addWorkerUnit, type DoctorVerdict, doctorVerdict, removeWorkerUnit, workerSharesQuery, type WorkerShares } from "./worker-shares";

/** Why the doctor keeps the worker from a unit, in the hub's terms; null when it does not. */
function useBlocked(verdict: DoctorVerdict): string | null {
  const t = useTranslations("workers.sharing.blocked");
  if (verdict.state === "none") return t("none");
  if (verdict.state === "stale") return t("stale", { at: new Date(verdict.at).toISOString().slice(0, 16).replace("T", " ") });
  if (verdict.state === "high") return t("high", { count: verdict.count });
  return null;
}

/** The newest doctor report: when it came and what it found by severity, a pill only for what is wrong. */
function DoctorReport({ entry, verdict }: { entry: WorkerShares; verdict: DoctorVerdict }) {
  const t = useTranslations("workers.sharing.doctor");
  return (
    <div className="flex min-w-0 items-start gap-2.5 text-sm" data-testid="worker-doctor" data-state={verdict.state}>
      <Stethoscope className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1">
        <span className="font-medium">{t("title")}</span>
        {entry.doctor_at ? (
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-muted-foreground">
            <Ago value={entry.doctor_at} never="" />
            <span data-testid="worker-doctor-counts">{t("counts", { high: entry.doctor_high ?? 0, medium: entry.doctor_medium ?? 0 })}</span>
            {verdict.state === "high" ? (
              <Badge variant="destructive" data-testid="worker-doctor-high">
                {t("high", { count: verdict.count })}
              </Badge>
            ) : null}
            {verdict.state === "stale" ? (
              <Badge variant="warning" data-testid="worker-doctor-stale">
                {t("stale")}
              </Badge>
            ) : null}
          </span>
        ) : (
          <span className="text-muted-foreground" data-testid="worker-doctor-none">
            {t("none")}
          </span>
        )}
      </div>
    </div>
  );
}

/** The units the worker is narrowed to, each with a way to take it back, then a unit to add, or why none may be. */
function Units({ worker, entry, blocked }: { worker: Worker; entry: WorkerShares; blocked: string | null }) {
  const t = useTranslations("workers.sharing");
  const ids = useId();
  const failure = useWorkerFailure();
  const chart = useQuery(shareChartQuery(browserApi));
  const [unit, setUnit] = useState("");
  const add = useWorkerWrite((api, slug: string) => addWorkerUnit(api, worker.id, slug));
  const remove = useWorkerWrite((api, slug: string) => removeWorkerUnit(api, worker.id, slug));
  const pending = add.isPending || remove.isPending;
  const choices = useMemo(() => {
    const today = new Date().toISOString().slice(0, 10);
    return (chart.data?.units ?? [])
      .filter((item) => item.archived_at === null && (item.ends_on === null || item.ends_on >= today) && !entry.units.includes(item.slug))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [chart.data, entry.units]);
  const names = new Map((chart.data?.units ?? []).map((item) => [item.slug, item.name]));

  const write = async (slug: string, adding: boolean) => {
    if (pending) return;
    try {
      await (adding ? add : remove).mutateAsync(slug);
    } catch (error) {
      const message = isApiError(error) && error.status === 409 ? error.info.message : null;
      const failed = failure(error);
      notifyFailure(t(adding ? "addFailed" : "removeFailed", { unit: slug }), message ? { ...failed, detail: message } : failed);
      return;
    }
    if (adding) setUnit("");
    notify({
      tone: "success",
      text: t(adding ? "added" : "removed", { unit: slug, name: worker.name }),
      description: t(adding ? "addedText" : entry.units.length === 1 ? "removedLastText" : "removedText"),
    });
  };

  return (
    <div className="flex flex-col gap-3" data-testid="worker-units">
      {entry.units.length === 0 ? (
        <p className="text-sm text-pretty text-muted-foreground" data-testid="worker-units-empty">
          {t("noUnits")}
        </p>
      ) : (
        <ul className="flex flex-wrap gap-2" aria-label={t("unitsLabel")}>
          {entry.units.map((slug) => (
            <li key={slug} className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-md border bg-card py-0.5 pr-0.5 pl-2 text-sm" data-unit={slug}>
              <Building2 className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
              <span className="min-w-0 [overflow-wrap:anywhere]">{names.get(slug) ?? slug}</span>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                onClick={() => void write(slug, false)}
                disabled={pending}
                aria-label={t("removeLabel", { unit: slug })}
                data-testid="worker-unit-remove"
              >
                <X aria-hidden="true" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="flex max-w-2xl flex-col gap-1.5"
        onSubmit={(event) => {
          event.preventDefault();
          if (unit && !blocked) void write(unit, true);
        }}
      >
        <Label htmlFor={`${ids}-unit`}>{t("addLabel")}</Label>
        <div className="flex min-w-0 flex-wrap gap-2">
          <NativeSelect
            id={`${ids}-unit`}
            value={unit}
            onChange={(event) => setUnit(event.target.value)}
            disabled={Boolean(blocked) || pending || !chart.isSuccess || choices.length === 0}
            className="min-w-0 flex-1 basis-48 md:[&_select]:h-8"
            aria-describedby={blocked ? `${ids}-blocked` : undefined}
            data-testid="worker-unit-select"
          >
            <NativeSelectOption value="">
              {chart.isPending ? t("loading") : chart.isError ? t("chartFailed") : choices.length === 0 ? t("noChoice") : t("pick")}
            </NativeSelectOption>
            {choices.map((item) => (
              <NativeSelectOption key={item.slug} value={item.slug}>
                {t("unitOption", { name: item.name, slug: item.slug })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
          <Button type="submit" variant="outline" busy={add.isPending} disabled={Boolean(blocked) || !unit || pending} data-testid="worker-unit-add">
            <Plus aria-hidden="true" />
            {t("add")}
          </Button>
        </div>
        {blocked ? (
          <p id={`${ids}-blocked`} className="text-xs text-pretty text-attention" data-testid="worker-sharing-blocked">
            {blocked}
          </p>
        ) : null}
      </form>
    </div>
  );
}

/**
 * The owner's Sharing card of a worker (plan credential-sharing): the switch that shares it, the newest report of its
 * doctor next to it, and the units of the org chart it is narrowed to, with Add and Remove. The hub narrows a worker
 * only while its doctor's newest report is under 48 hours old and has no high finding, so Add, and the switch of a
 * worker narrowed to units, stay off with the reason until it is.
 */
export function WorkerSharing({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.sharing");
  const query = useQuery(workerSharesQuery(browserApi));
  const entry = query.data?.find((item) => item.id === worker.id) ?? null;
  const now = useNow(entry?.doctor_at != null);
  const verdict = entry ? doctorVerdict(entry, now) : ({ state: "ok" } as const);
  const blocked = useBlocked(verdict);
  return (
    <SectionCard title={t("title")} description={t("description")} testId="worker-sharing">
      <div className="flex flex-col gap-4">
        <SharedSwitch worker={worker} locked={entry && entry.units.length > 0 ? blocked : null} className="mt-0 border-t-0 pt-0" />
        {query.isPending ? (
          <p className="text-sm text-muted-foreground" aria-busy="true" data-testid="worker-sharing-loading">
            {t("loading")}
          </p>
        ) : query.isError ? (
          <p className="text-sm text-danger" role="alert" data-testid="worker-sharing-error">
            {t("loadFailed")}
          </p>
        ) : entry ? (
          <>
            <DoctorReport entry={entry} verdict={verdict} />
            <Units worker={worker} entry={entry} blocked={blocked} />
          </>
        ) : null}
      </div>
    </SectionCard>
  );
}
