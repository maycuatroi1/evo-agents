"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Tag } from "@/components/data/identifier";
import type { AutonomyPolicy } from "@/lib/merge-queue";
import { cn } from "@/lib/utils";

import { Panel } from "./chrome";
import { isDefault, policyRepos, readPath, REPO_KEYS, SECTION_NAMES, SECTIONS } from "./policy-model";
import { useKeyLabel, useValueText } from "./policy-value";

function DefaultTag() {
  const t = useTranslations("policy");
  return (
    <Tag className="ml-2 align-middle" data-testid="policy-default">
      {t("default")}
    </Tag>
  );
}

function Facts({ children }: { children: ReactNode }) {
  return <dl className="grid grid-cols-1 gap-x-4 gap-y-2.5 px-4 py-3 text-sm sm:grid-cols-[minmax(10rem,max-content)_minmax(0,1fr)]">{children}</dl>;
}

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-xs leading-5 text-muted-foreground sm:text-[13px]">{label}</dt>
      <dd className="-mt-2 min-w-0 [overflow-wrap:anywhere] sm:mt-0" data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

/** Each repo of the project as the policy reads it: auto or off, its target branch, its PROD release and its jobs. */
function ReposPanel({ policy }: { policy: AutonomyPolicy }) {
  const t = useTranslations("policy");
  const label = useKeyLabel();
  const value = useValueText();
  const repos = policyRepos(policy.effective);
  const auto = repos.filter((repo) => readPath(policy.effective, `repos.${repo}.mode`) === "auto").length;
  return (
    <Panel title={t("repos.title")} action={<span className="text-xs text-fg-subtle">{t("repos.autoCount", { count: auto })}</span>} testId="policy-repos" className="lg:col-span-2">
      {repos.length === 0 ? (
        <p className="px-4 py-3 text-[13px] text-muted-foreground">{t("repos.none")}</p>
      ) : (
        <ul className="flex flex-col">
          {repos.map((repo) => {
            const at = (path: string) => readPath(policy.effective, `repos.${repo}.${path}`);
            const mode = at("mode");
            const branchSet = readPath(policy.policy, `repos.${repo}.target_branch`) !== undefined;
            return (
              <li key={repo} className="flex min-w-0 flex-col gap-2 border-t px-4 py-3 first:border-t-0" data-testid="policy-repo" data-repo={repo} data-mode={String(mode)}>
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="min-w-0 truncate font-mono text-[13px] font-medium">{repo}</span>
                  <Tag className={cn(mode === "auto" && "bg-brand-soft text-brand")} data-testid="policy-repo-mode">
                    {value(`repos.${repo}.mode`, mode)}
                  </Tag>
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[13px] sm:grid-cols-3 xl:grid-cols-4">
                  {REPO_KEYS.filter((spec) => spec.path !== "mode").map((spec) => {
                    const shown = at(spec.path);
                    const mono = typeof shown === "string" && (spec.path === "target_branch" || spec.path.startsWith("jobs."));
                    return (
                      <div key={spec.path} className="flex min-w-0 flex-col gap-0.5" data-key={spec.path}>
                        <dt className="text-xs text-muted-foreground">{label(spec.path)}</dt>
                        <dd className={cn("min-w-0 [overflow-wrap:anywhere]", mono && "font-mono text-xs leading-5", shown === null && "text-fg-subtle")}>
                          {mono ? String(shown) : value(spec.path, shown)}
                          {spec.path === "target_branch" && !branchSet ? <span className="font-sans text-fg-subtle"> {t("defaultBranch")}</span> : null}
                        </dd>
                      </div>
                    );
                  })}
                </dl>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

/**
 * The policy at a revision, every key at the value it reads: the repos first, then the merge, the Judge, the breaker
 * and the audit samples. A key at its default says so, so an admin sees what the revision set and what it left.
 */
export function PolicyView({ policy }: { policy: AutonomyPolicy }) {
  const t = useTranslations("policy");
  const label = useKeyLabel();
  const value = useValueText();
  return (
    <div className="grid items-start gap-4 lg:grid-cols-2" data-testid="policy-view" data-revision={policy.revision}>
      <ReposPanel policy={policy} />
      {SECTION_NAMES.map((section) => (
        <Panel key={section} title={t(`sections.${section}`)} testId={`policy-${section}`}>
          <Facts>
            {SECTIONS[section].map((spec) => {
              const shown = readPath(policy.effective, spec.path);
              return (
                <Fact key={spec.path} label={label(spec.path)} testId={`policy-value-${spec.path}`}>
                  {Array.isArray(shown) && shown.length > 0 ? (
                    <ul className="flex flex-col gap-0.5 font-mono text-xs">
                      {shown.map((glob) => (
                        <li key={String(glob)}>{String(glob)}</li>
                      ))}
                    </ul>
                  ) : (
                    value(spec.path, shown)
                  )}
                  {isDefault(spec, shown) ? <DefaultTag /> : null}
                </Fact>
              );
            })}
          </Facts>
        </Panel>
      ))}
    </div>
  );
}
