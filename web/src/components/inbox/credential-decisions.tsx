"use client";

import { KeyRound, LockKeyhole, ShieldAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import {
  type CredentialRequest,
  credentialOption,
  credentialRequest,
  footWords,
  givesValue,
  grantsAccount,
  MAX_SECRET_BYTES,
  sendWords,
  type ValueProblem,
  valueProblem,
} from "./credential-model";
import { type AnswerHandlers, useAnswerEffects } from "./hooks";
import { answerBody, isHubDecision } from "./model";
import { type AnswerRequest, answerDecision, type Decision } from "./queries";

/**
 * A credential request on the DecisionCard and the phone's decision screen (docs/credentials.md, "A run that asks for a
 * credential"): what the run asks for among the question's grounds, and in the owner's answer form a password field for
 * the variable's value and a warning when the answer lets the run act as them. The value field is write-only, as on
 * the Secrets page: a password input without a `name`, never filled by the page, read when the form is sent and emptied
 * at that moment whatever the hub answers; the answer that carries it goes through plain state, not a mutation whose
 * cache would keep the body (`useAnswerWithSecret`).
 */

const code = (chunks: ReactNode) => <span className="font-mono text-[0.92em] text-foreground [overflow-wrap:anywhere]">{chunks}</span>;

/** The secret that keeps the variable's value: the name the request proposes, the hub's default when it named none. */
function secretName(request: CredentialRequest): string {
  return request.secret_name ?? (request.env_var ?? "").toLowerCase().replaceAll("_", "-");
}

/**
 * The answer to a credential request that carries the value pasted, which does what `useAnswerDecision` does but not
 * through TanStack Query's mutations, which keep their variables (here the value) in the mutation cache after the
 * call, as the Secrets page's PUT (`components/secrets/hooks.ts`): the body lives only for the call, and the hook keeps
 * no more than whether one runs.
 */
function useAnswerWithSecret(decision: Pick<Decision, "id" | "project" | "plan_id">, handlers: AnswerHandlers = {}) {
  const effects = useAnswerEffects(decision, handlers);
  const [pending, setPending] = useState(false);
  /** Whether the hub took the answer; a refusal went to `onFailed` as with the mutation. */
  const send = async (body: AnswerRequest & { secret: string }): Promise<boolean> => {
    setPending(true);
    try {
      effects.answered(await answerDecision(browserApi(), decision.project, decision.id, body));
      return true;
    } catch (error) {
      await effects.failed(error);
      return false;
    } finally {
      setPending(false);
      await effects.settled();
    }
  };
  return { send, pending };
}

/** The value field of the answer form whose ids are `ids`, read and emptied when the form is sent, never in render. */
function valueField(ids: string): HTMLInputElement | null {
  const found = document.getElementById(`${ids}-secret`);
  return found instanceof HTMLInputElement ? found : null;
}

/**
 * The credential parts of the owner's answer form whose ids are `ids`: whether the decision is a credential request,
 * what is wrong with the value, the words of the send button, and the answer sent with the value.
 */
export function useCredentialAnswer(decision: Decision, handlers: AnswerHandlers, ids: string) {
  const t = useTranslations("inbox.credential");
  const request = credentialRequest(decision);
  const [problem, setProblem] = useState<ValueProblem>(null);
  // The field is emptied as the answer goes; after a refusal it says to paste the value again.
  const [forgotten, setForgotten] = useState(false);
  const write = useAnswerWithSecret(decision, handlers);
  return {
    request,
    /** The option the form opens on when the agent recommends none (`credentialOption`). */
    initial: credentialOption(decision),
    problem,
    forgotten,
    pending: write.pending,
    /** The owner typed in the field: what was said of it no longer holds. */
    typed: () => {
      setProblem(null);
      setForgotten(false);
    },
    /** The send button's words for `option`, what the answer gives; null for the form's usual Send answer. */
    label: (option: string | null): string | null => {
      const words = request ? sendWords(option) : null;
      return words ? t(words) : null;
    },
    /** Whether the value keeps the answer from going; if so it says why beside the field and focuses it. */
    blocks: (option: string | null, text: string): boolean => {
      if (!request?.env_var) return false;
      const found = valueProblem(option, valueField(ids)?.value ?? "", text.trim());
      setProblem(found);
      if (found) valueField(ids)?.focus();
      return found !== null;
    },
    /** Send the answer with the value, the field emptied first; false, sending nothing, when the answer carries none. */
    send: (option: string | null, text: string): boolean => {
      const field = valueField(ids);
      if (!request?.env_var || !givesValue(option) || !field) return false;
      const secret = field.value;
      field.value = "";
      setForgotten(false);
      void write.send({ ...answerBody(option, text), secret }).then((answered) => setForgotten(!answered));
      return true;
    },
  };
}

export type CredentialAnswer = ReturnType<typeof useCredentialAnswer>;

/** What the credential parts read of the answer form (`useAnswerForm` of decision-view.tsx). */
export type CredentialForm = { decision: Decision; ids: string; option: string | null; parked: boolean; credential: CredentialAnswer };

/**
 * What a credential request asks for, the first of the question's grounds: the variable and the secret that will keep
 * its value, the hub account of the run's owner, or both. Nothing for any other decision.
 */
export function CredentialNeeds({ decision, text }: { decision: Decision; text: string }) {
  const t = useTranslations("inbox.credential");
  const request = credentialRequest(decision);
  if (!request) return null;
  const words = request.env_var && request.hub_account ? "asksBoth" : request.env_var ? "asksValue" : "asksAccount";
  return (
    <div className="flex min-w-0 items-start gap-2.5" data-testid="decision-credential-needs">
      <KeyRound className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <p className={cn("min-w-0 text-pretty text-foreground [overflow-wrap:anywhere]", text)}>
        {t.rich(words, { name: request.env_var ?? "", secret: secretName(request), owner: decision.owner, code })}
      </p>
    </div>
  );
}

/**
 * The owner's fields for the option chosen, under the options: for one that gives the variable's value, its password
 * field (16 px on the phone's screen, so the phone does not zoom into it) with where the value goes and what is wrong
 * with it; for one that lets the run act as the owner, what that lets its agent do. Nothing for any other option.
 */
export function CredentialFields({ form, screen = false }: { form: CredentialForm; screen?: boolean }) {
  const t = useTranslations("inbox.credential");
  const format = useFormatter();
  const { decision, ids, option, credential } = form;
  const request = credential.request;
  const variable = request?.env_var && givesValue(option) ? request.env_var : null;
  const account = Boolean(request?.hub_account) && grantsAccount(option);
  if (!request || (!variable && !account)) return null;
  const field = `${ids}-secret`;
  const describedBy = [`${field}-hint`, credential.problem ? `${field}-problem` : null, credential.forgotten ? `${field}-forgotten` : null];
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="credential-fields">
      {variable ? (
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor={field} className={cn("font-medium text-muted-foreground", screen ? "text-sm" : "text-xs")}>
            {t.rich("value", { name: variable, code })}
          </Label>
          <div className="relative">
            <LockKeyhole className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
            {/* No name attribute: the value is never part of a form submission the browser could make by itself. */}
            <Input
              id={field}
              type="password"
              defaultValue=""
              onChange={credential.typed}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              className={cn("pl-8 font-mono", screen ? "h-11 text-base md:text-base" : "md:h-9 md:text-[13px]")}
              aria-invalid={credential.problem ? true : undefined}
              aria-describedby={describedBy.filter(Boolean).join(" ")}
              aria-required="true"
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              data-form-type="other"
              data-testid="credential-value"
            />
          </div>
          <p id={`${field}-hint`} className="text-xs text-pretty text-muted-foreground" data-testid="credential-value-hint">
            {t.rich("valueHint", { secret: secretName(request), project: decision.project, code })}
          </p>
          {credential.problem ? (
            <p id={`${field}-problem`} className="text-sm font-medium text-danger" role="alert" data-testid="credential-value-problem">
              {t(`problem.${credential.problem}`, { name: variable, max: format.number(MAX_SECRET_BYTES) })}
            </p>
          ) : null}
          {credential.forgotten ? (
            <p id={`${field}-forgotten`} className="text-xs text-pretty text-muted-foreground" data-testid="credential-value-forgotten">
              {t("forgotten")}
            </p>
          ) : null}
        </div>
      ) : null}
      {account ? (
        <p
          className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-[13px] leading-[19px] text-attention"
          data-testid="credential-account-note"
        >
          <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span className="text-pretty">{t("accountNote")}</span>
        </p>
      ) : null}
    </div>
  );
}

/**
 * The line under the answer: that the agent goes on as soon as the owner answers, that the parked run resumes, or for a
 * credential request where the value goes and that the run acts as the owner.
 */
export function AnswerHint({ form, className }: { form: CredentialForm; className?: string }) {
  const t = useTranslations("inbox.answer");
  const tCredential = useTranslations("inbox.credential");
  const words = form.credential.request ? footWords(form.option) : null;
  return (
    <p className={cn("text-xs text-pretty text-fg-subtle", className)} data-testid="decision-form-hint">
      {words ? tCredential(words) : isHubDecision(form.decision) ? t("footHub") : form.parked ? t("footParked") : t("foot")}
    </p>
  );
}
