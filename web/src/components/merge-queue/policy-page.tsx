"use client";

import { useQuery } from "@tanstack/react-query";
import { Eye, History, Info, PencilLine } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { type AutonomyPolicy, mergeQueueHref, policyQuery, policyRevisionsQuery } from "@/lib/merge-queue";

import { MergeQueueHeader, TEXT_LINK, useMergeViewer } from "./chrome";
import { PolicyFormView } from "./policy-form";
import { ComparePanel, type PolicyLinks, RevisionsPanel } from "./policy-history";
import { readPolicyView } from "./policy-model";
import { PolicyView } from "./policy-view";

function usePolicyLinks(): PolicyLinks & { replace: (href: Route) => void } {
  const pathname = usePathname();
  const router = useRouter();
  const revision = useCallback((value: number | null) => (value === null ? pathname : `${pathname}?revision=${value}`) as Route, [pathname]);
  const compare = useCallback((from: number, to: number) => `${pathname}?from=${from}&to=${to}` as Route, [pathname]);
  const replace = useCallback((href: Route) => router.replace(href, { scroll: false }), [router]);
  return { revision, compare, replace };
}

/** Who wrote the revision shown, when, and why; or that the project has no policy yet. */
function Provenance({ policy }: { policy: AutonomyPolicy }) {
  const t = useTranslations("policy");
  if (policy.revision === 0) return <span data-testid="policy-provenance">{t("noPolicySub")}</span>;
  return (
    <span data-testid="policy-provenance">
      {t.rich("provenance", {
        revision: policy.revision,
        login: policy.created_by ?? "?",
        when: () => <When value={policy.created_at} short />,
      })}
      {policy.note ? ` ${t("noteQuoted", { note: policy.note })}` : ""}
    </span>
  );
}

function PolicyBody({ project, policy, revision, admin, reader }: { project: string; policy: AutonomyPolicy; revision: number | null; admin: boolean; reader: boolean }) {
  const t = useTranslations("policy");
  const params = useSearchParams();
  const view = readPolicyView(params);
  const links = usePolicyLinks();
  const revisions = useQuery(policyRevisionsQuery(browserApi, project));
  const list = revisions.data ?? [];
  const newest = list[0]?.revision ?? policy.revision;
  const to = view.to ?? newest;
  const from = view.from ?? Math.max(0, to - 1);
  return (
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
      <div className="flex min-w-0 flex-col gap-4">
        {revision !== null ? (
          <Banner
            icon={History}
            title={t("oldRevision", { revision: policy.revision })}
            actions={
              <Link href={links.revision(null)} className={TEXT_LINK} data-testid="policy-show-newest">
                {t("showNewest")}
              </Link>
            }
            testId="policy-old-revision"
          />
        ) : policy.revision === 0 ? (
          <Banner icon={Info} title={t("noPolicyTitle")} testId="policy-none">
            {admin ? t("noPolicyAdmin") : t("noPolicyReader")}
          </Banner>
        ) : null}
        {reader ? (
          <Banner icon={Eye} title={t("readOnlyTitle")} testId="policy-read-only">
            {t("readOnly")}
          </Banner>
        ) : null}
        <PolicyView policy={policy} />
      </div>
      <div className="flex min-w-0 flex-col gap-4">
        <RevisionsPanel revisions={list} shown={policy.revision} links={links} />
        {list.length > 0 ? (
          <ComparePanel project={project} revisions={list} from={from} to={to} onChange={(nextFrom, nextTo) => links.replace(links.compare(nextFrom, nextTo))} />
        ) : null}
      </div>
    </div>
  );
}

/**
 * A project's autonomy policy: what the hub may do on its own in the project's repos, above all whether it merges
 * their merge requests on GitLab. Any member reads it, at its newest revision or an older one (`?revision=N`), with
 * every revision and a comparison of two (`?from=N&to=M`); an admin of the project writes the next revision through the
 * form (`?edit=1`).
 */
export function PolicyPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("policy");
  const params = useSearchParams();
  const { revision, edit } = readPolicyView(params);
  const viewer = useMergeViewer(project);
  const admin = viewer?.admin ?? false;
  const editing = edit && admin;
  const state = useHubQuery(policyQuery(browserApi, project, editing ? null : revision), initialError);
  const shown = state.status === "success" ? state.data : null;
  return (
    <div className="flex flex-col gap-6" data-testid="policy-page" data-mode={editing ? "edit" : "view"}>
      <MergeQueueHeader
        project={project}
        current="policy"
        sub={shown ? <Provenance policy={shown} /> : null}
        actions={
          admin && !editing ? (
            <Button asChild variant="secondary">
              <Link href={`${mergeQueueHref(project, "policy")}?edit=1` as Route} data-testid="policy-edit">
                <PencilLine aria-hidden="true" />
                {shown?.revision === 0 ? t("create") : t("edit")}
              </Link>
            </Button>
          ) : null
        }
      />
      <QueryView state={state} loading={<PageSkeleton />}>
        {(policy) =>
          editing ? (
            <PolicyFormView project={project} policy={policy} />
          ) : (
            <PolicyBody project={project} policy={policy} revision={revision} admin={admin} reader={viewer !== null && !admin} />
          )
        }
      </QueryView>
    </div>
  );
}
