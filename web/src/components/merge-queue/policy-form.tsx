"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { type AutonomyPolicy, mergeKeys, mergeQueueHref, type PolicyWrite, writePolicy } from "@/lib/merge-queue";
import { projectQuery } from "@/lib/queries";

import { fieldId, Group, KeyField } from "./policy-fields";
import {
  KEYS,
  MAX_NOTE_CHARS,
  onGithub,
  policyBody,
  policyForm,
  type PolicyForm,
  type PolicyProblems,
  problemCount,
  readPath,
  REPO_KEYS,
  type RepoFacts,
  SECTION_NAMES,
  SECTIONS,
} from "./policy-model";

function useWritePolicy(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PolicyWrite) => writePolicy(browserApi(), project, body),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: mergeKeys.all(project) }),
  });
}

/** The first wrong field, to give it focus: the policy's own keys in order, then each repo's. */
function firstWrong(base: string, problems: PolicyProblems, repos: string[]): string | null {
  const global = KEYS.find((spec) => problems.global[spec.path]);
  if (global) return fieldId(base, "policy", global.path);
  for (const [index, repo] of repos.entries()) {
    const spec = REPO_KEYS.find((key) => problems.repos[repo]?.[key.path]);
    if (spec) return fieldId(base, `repo${index}`, spec.path);
  }
  return problems.note ? `${base}-note` : null;
}

/**
 * The policy as a form, for an admin of the project: each repo's mode, target branch, PROD release and CI jobs, then
 * the merge, the Judge, the breaker and the audit samples. Every field repeats the hub's checks, so nothing goes out
 * while one is wrong, and the first wrong field takes focus. Saving writes the next revision, at the revision the form
 * was opened from (if_revision), so a policy changed meanwhile is never overwritten; a policy equal to the newest
 * writes nothing. A refusal of the hub shows above the buttons.
 */
export function PolicyFormView({ project, policy }: { project: string; policy: AutonomyPolicy }) {
  const t = useTranslations("policy.form");
  const tPolicy = useTranslations("policy");
  const base = useId();
  const router = useRouter();
  const write = useWritePolicy(project);
  const failure = useWriteFailure();
  const projectInfo = useQuery(projectQuery(browserApi, project));
  const [form, setForm] = useState<PolicyForm>(() => policyForm(policy));
  const [problems, setProblems] = useState<PolicyProblems | null>(null);
  const [error, setError] = useState<WriteFailure | null>(null);
  const repos = Object.keys(form.repos);
  // Where each repo lives and whether the project still lists it; every repo counts as listed until the project is read.
  const listed = projectInfo.data?.repos ?? null;
  const facts: Record<string, RepoFacts & { branch: string | null }> = {};
  for (const repo of repos) {
    const info = listed?.find((item) => item.name === repo);
    facts[repo] = { origin: info?.origin ?? null, listed: listed === null || info !== undefined, branch: info?.default_branch ?? null };
  }

  const setGlobal = (path: string, value: string) => setForm((current) => ({ ...current, global: { ...current.global, [path]: value } }));
  const setRepo = (repo: string, path: string, value: string) =>
    setForm((current) => ({ ...current, repos: { ...current.repos, [repo]: { ...current.repos[repo], [path]: value } } }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (write.isPending) return;
    const result = policyBody(form, policy.policy, facts);
    if (result.problems) {
      setProblems(result.problems);
      const first = firstWrong(base, result.problems, repos);
      if (first) document.getElementById(first)?.focus();
      return;
    }
    setProblems(null);
    setError(null);
    write.mutate(
      { policy: result.body, note: form.note.trim() || null, if_revision: policy.revision },
      {
        onSuccess: (saved) => {
          notify(
            saved.changed
              ? { tone: "success", text: t("saved", { revision: saved.revision }), description: t("savedText") }
              : { tone: "info", text: t("unchanged"), description: t("unchangedText") },
          );
          router.push(mergeQueueHref(project, "policy"));
        },
        onError: (failed) => setError(failure(failed, { 403: t("forbidden"), 409: t("conflict"), 422: t("refused") })),
      },
    );
  };

  const count = problems ? problemCount(problems) : 0;
  return (
    <form noValidate onSubmit={submit} className="flex flex-col gap-4" aria-label={t("label")} data-testid="policy-form">
      <Group title={tPolicy("repos.title")} description={t("hints.repos")} testId="policy-form-repos">
        {repos.length === 0 ? <p className="text-[13px] text-muted-foreground md:col-span-2 2xl:col-span-4">{tPolicy("repos.none")}</p> : null}
        {repos.map((repo, index) => {
          const scope = `repo${index}`;
          const github = onGithub(facts[repo]?.origin);
          return (
            <fieldset
              key={repo}
              className="grid min-w-0 gap-4 rounded-sm border px-3 py-3 md:col-span-2 md:grid-cols-2 2xl:col-span-4 2xl:grid-cols-4"
              data-testid="policy-form-repo"
              data-repo={repo}
            >
              <legend className="px-1 font-mono text-[13px] font-medium">{repo}</legend>
              {facts[repo] && !facts[repo].listed ? (
                <p className="text-xs text-attention md:col-span-2 2xl:col-span-4" role="note">
                  {t("unlisted", { repo })}
                </p>
              ) : null}
              {REPO_KEYS.map((spec) => (
                <KeyField
                  key={spec.path}
                  base={base}
                  scope={scope}
                  spec={spec}
                  value={form.repos[repo]?.[spec.path] ?? ""}
                  onChange={(value) => setRepo(repo, spec.path, value)}
                  problem={problems?.repos[repo]?.[spec.path]}
                  placeholder={spec.path === "target_branch" ? (facts[repo]?.branch ?? String(readPath(policy.effective, `repos.${repo}.target_branch`) ?? "")) : undefined}
                  disabledOptions={spec.path === "mode" && github && form.repos[repo]?.mode !== "auto" ? ["auto"] : []}
                  hint={spec.path === "mode" && github ? t("hints.github") : undefined}
                />
              ))}
            </fieldset>
          );
        })}
      </Group>
      {SECTION_NAMES.map((section) => (
        <Group key={section} title={tPolicy(`sections.${section}`)} description={t(`hints.${section}`)} testId={`policy-form-${section}`}>
          {SECTIONS[section].map((spec) => (
            <KeyField
              key={spec.path}
              base={base}
              scope="policy"
              spec={spec}
              value={form.global[spec.path] ?? ""}
              onChange={(value) => setGlobal(spec.path, value)}
              problem={problems?.global[spec.path]}
            />
          ))}
        </Group>
      ))}
      <div className="flex flex-col gap-1.5 rounded-md border bg-card px-4 py-4 shadow-raised">
        <Label htmlFor={`${base}-note`}>{t("note")}</Label>
        <Textarea
          id={`${base}-note`}
          rows={2}
          value={form.note}
          maxLength={MAX_NOTE_CHARS}
          onChange={(event) => setForm((current) => ({ ...current, note: event.target.value }))}
          aria-describedby={`${base}-note-hint`}
          aria-invalid={problems?.note ? true : undefined}
          data-testid="policy-note"
        />
        <p id={`${base}-note-hint`} className="text-xs text-fg-subtle">
          {t("noteHint", { max: MAX_NOTE_CHARS })}
        </p>
      </div>
      {count > 0 ? (
        <p role="alert" className="text-sm text-danger" data-testid="policy-form-problems">
          {t("problems", { count })}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-4">
        <Button asChild variant="outline">
          <Link href={mergeQueueHref(project, "policy")} data-testid="policy-cancel">
            {t("cancel")}
          </Link>
        </Button>
        <Button type="submit" busy={write.isPending} data-testid="policy-save">
          <Save aria-hidden="true" />
          {write.isPending ? t("saving") : t("save")}
        </Button>
      </div>
    </form>
  );
}
