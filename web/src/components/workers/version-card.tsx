"use client";

import { ClockAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier } from "@/components/data/identifier";
import { useNow } from "@/hooks/use-now";

import { Ago } from "./ago";
import { Note } from "./note";
import type { Worker } from "./queries";
import { SectionCard } from "./section-card";
import { commitUrl, shortCommit, staleNightly, type UpdateProblem, updateProblems, updateState } from "./self-update";
import { UPDATE_LOOKS } from "./update-badge";
import { UpdateNow } from "./update-now";

/** A commit of evo-agents as a chip: its first 7 characters, a link to its page on GitHub, and a copy button. */
function Commit({ sha, testId }: { sha: string; testId: string }) {
  const t = useTranslations("workers.version");
  const href = commitUrl(sha);
  return (
    <Identifier
      value={sha}
      title={sha}
      externalHref={href}
      linkLabel={href ? t("commitLink", { commit: shortCommit(sha) }) : undefined}
      copy
      copyLabel={t("copyCommit")}
      testId={testId}
    >
      {shortCommit(sha)}
    </Identifier>
  );
}

/** One line in the muted voice under a value: when it was checked, since when it waits. */
function Aside({ children }: { children: ReactNode }) {
  return <span className="block text-xs text-muted-foreground">{children}</span>;
}

/** What the Updates line says: the owner's lock first, then what the daemon last said of the tag nightly. */
function Updates({ worker, isOwner }: { worker: Worker; isOwner: boolean }) {
  const t = useTranslations("workers.version.updates");
  const format = useFormatter();
  const report = worker.self_update;
  const state = updateState(worker);
  const nightly = report?.nightly_commit ? <Commit sha={report.nightly_commit} testId="worker-nightly-commit" /> : null;
  let line: ReactNode;
  let aside: ReactNode = null;
  switch (state) {
    case "none":
      line = <span className="text-muted-foreground">{t("none")}</span>;
      break;
    case "locked":
      line = isOwner ? t("locked") : t("lockedBy", { owner: worker.owner });
      break;
    case "unreported":
      line = <span className="text-muted-foreground">{t("unreported")}</span>;
      break;
    case "waiting":
    case "updating":
      line = nightly ? (
        <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1">
          {t(`${state}To`)} {nightly}
        </span>
      ) : (
        t(state)
      );
      if (state === "waiting" && report?.since) {
        aside = t("since", { time: format.dateTime(new Date(report.since), { dateStyle: "medium", timeStyle: "short" }) });
      }
      break;
    case "other":
      line = <span className="font-mono text-xs">{report?.state}</span>;
      break;
    default:
      line = t(state);
  }
  if (state === "current" && report?.checked_at) {
    aside = (
      <>
        {t("checked")} <Ago value={report.checked_at} never="" />
      </>
    );
  }
  return (
    <dd className="min-w-0" data-testid="worker-updates" data-state={state}>
      {line}
      {aside ? <Aside>{aside}</Aside> : null}
    </dd>
  );
}

/**
 * Where the tag nightly pointed when the hub last read it, the same for every worker. The hub reads it once a minute, so
 * the line says more only when that read is over 10 minutes old: the hub has not reached GitHub since, in amber.
 */
function HubNightly({ nightly }: { nightly: Worker["nightly"] }) {
  const t = useTranslations("workers.version");
  const format = useFormatter();
  const now = useNow(nightly !== null);
  const stale = staleNightly(nightly, now);
  if (!nightly) {
    return (
      <dd className="text-muted-foreground" data-testid="worker-hub-nightly">
        {t("nightlyUnread")}
      </dd>
    );
  }
  return (
    <dd className="min-w-0" data-testid="worker-hub-nightly" data-stale={stale ? "true" : undefined}>
      <Commit sha={nightly.commit} testId="worker-hub-nightly-commit" />
      {stale ? (
        <span className="mt-1 flex items-start gap-1.5 text-xs text-attention" data-testid="worker-hub-nightly-stale">
          <ClockAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span title={format.dateTime(new Date(stale.read_at), { dateStyle: "medium", timeStyle: "medium" })}>
            {t("nightlyStale", { time: format.relativeTime(new Date(stale.read_at), now ?? undefined) })}
          </span>
        </span>
      ) : null}
    </dd>
  );
}

/**
 * The evo-agents a worker runs: its release, the commit its install was built from (a link to GitHub), the commit
 * nightly points to as the hub read it, where its updates stand, and Update now. Its owner and a hub admin see the
 * same card, but only the owner asks for Update now; the owner locks the version in Settings.
 */
export function VersionCard({ worker, isOwner }: { worker: Worker; isOwner: boolean }) {
  const t = useTranslations("workers.version");
  return (
    <SectionCard title={t("title")} description={t("description")} testId="worker-version">
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-sm">
        <dt className="text-muted-foreground">{t("release")}</dt>
        <dd className="font-mono text-[13px] [overflow-wrap:anywhere]" data-testid="worker-agent-version">
          {worker.agent_version}
        </dd>
        <dt className="text-muted-foreground">{t("commit")}</dt>
        <dd className="min-w-0" data-testid="worker-commit">
          {worker.agent_commit ? (
            <Commit sha={worker.agent_commit} testId="worker-commit-sha" />
          ) : (
            <>
              <span className="text-muted-foreground">{t("noCommit")}</span>
              {worker.last_heartbeat_at ? <Aside>{t("noCommitWhy")}</Aside> : null}
            </>
          )}
        </dd>
        <dt className="text-muted-foreground">{t("updatesLabel")}</dt>
        <Updates worker={worker} isOwner={isOwner} />
        <dt className="text-muted-foreground">{t("nightlyLabel")}</dt>
        <HubNightly nightly={worker.nightly} />
      </dl>
      <UpdateNow worker={worker} isOwner={isOwner} />
    </SectionCard>
  );
}

/**
 * What went wrong with the worker's updates, as notes above its cards, one per problem; nothing while its updates go
 * as they should. The daemon's own words (its error, why it cannot update itself) are shown as it sent them.
 */
export function UpdateNotes({ worker, isOwner }: { worker: Worker; isOwner: boolean }) {
  const t = useTranslations("workers.update.note");
  const format = useFormatter();
  const report = worker.self_update;
  const now = useNow(report?.state === "waiting");
  if (!report) return null;
  const when = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium", timeStyle: "short" });
  const note = (problem: UpdateProblem): ReactNode => {
    const look = UPDATE_LOOKS[problem];
    const tone = look.tone === "danger" ? "danger" : "warning";
    const testId = `worker-update-${problem}`;
    if (problem === "failed") {
      return (
        <Note key={problem} tone={tone} icon={look.icon} testId={testId}>
          <p className="font-medium">{report.error_at ? t("failedAt", { time: when(report.error_at) }) : t("failed")}</p>
          <p className="font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]" data-testid="worker-update-error">
            {report.error}
          </p>
          <p>{t("failedNext")}</p>
        </Note>
      );
    }
    if (problem === "unsupported") {
      return (
        <Note key={problem} tone={tone} icon={look.icon} testId={testId}>
          <p className="font-medium">{t("unsupported")}</p>
          {report.reason ? (
            <p className="[overflow-wrap:anywhere]" data-testid="worker-update-reason">
              {report.reason}
            </p>
          ) : null}
          <p>{t("unsupportedNext")}</p>
        </Note>
      );
    }
    return (
      <Note key={problem} tone={tone} icon={look.icon} testId={testId}>
        <p className="font-medium">{t("waiting", { time: report.since ? when(report.since) : "" })}</p>
        <p>{isOwner ? t("waitingOwner") : t("waitingNext")}</p>
      </Note>
    );
  };
  return <>{updateProblems(worker, now).map(note)}</>;
}
