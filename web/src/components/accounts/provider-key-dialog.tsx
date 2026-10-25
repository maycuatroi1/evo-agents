"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { notify } from "@/components/feedback/toast";
import { browserApi } from "@/lib/api/browser";

import { FormDialog } from "./form-parts";
import { MAX_SECRET_BYTES, secretNamesQuery } from "./login-queries";
import { KeyField } from "./provider-fields";
import { useProviderFailure, useProviderWrite } from "./provider-hooks";
import { keyBody, type KeySource, type ProviderProblems, keySourceProblems } from "./provider-model";
import type { Account } from "./queries";

/**
 * Change key of a provider account: a new key pasted, or an env secret of the visitor's to copy it from. Nothing
 * else of the account changes. The field is emptied as the form is sent, before the hub answers.
 */
export function ProviderKeyDialog({ account, open, onOpenChange }: { account: Account; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("accounts.provider.keyForm");
  const tForm = useTranslations("accounts.provider.form");
  const tProblem = useTranslations("accounts.provider.form.problems");
  const ids = useId();
  const write = useProviderWrite();
  const failureOf = useProviderFailure();
  const secrets = useQuery(secretNamesQuery(browserApi));
  const keyRef = useRef<HTMLInputElement>(null);
  const [source, setSource] = useState<KeySource>("paste");
  const [secret, setSecret] = useState("");
  const [errors, setErrors] = useState<ProviderProblems>({});
  const [forgotten, setForgotten] = useState(false);
  const say = (field: "key" | "secret") => (errors[field] ? tProblem(errors[field], { max: MAX_SECRET_BYTES, name: account.name }) : null);

  const submit = async () => {
    const field = keyRef.current;
    const key = field?.value ?? "";
    const found = keySourceProblems({ source, secret }, key);
    setErrors(found);
    if (found.key || found.secret) {
      document.getElementById(`${ids}-${found.key ? "key" : "secret"}`)?.focus();
      return;
    }
    const body = keyBody({ source, secret }, key);
    if (field) field.value = "";
    setForgotten(false);
    const done = await write.change(account.name, body);
    if (!done.ok) {
      setForgotten(source === "paste");
      return;
    }
    notify({ tone: "success", text: t("saved", { name: account.name }), description: t("savedText") });
    onOpenChange(false);
  };

  const failure = write.error ? failureOf(write.error) : null;
  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("title", { name: account.name })}
      description={t("description")}
      pending={write.pending}
      failure={failure}
      labels={{ save: t("save"), saving: tForm("saving"), cancel: tForm("cancel") }}
      onSubmit={() => void submit()}
      testId="provider-key-dialog"
    >
      <KeyField
        ids={ids}
        source={source}
        secret={secret}
        secrets={secrets.data ?? []}
        keyRef={keyRef}
        keyHint=""
        keyProblem={say("key")}
        secretProblem={say("secret")}
        onSource={(next) => {
          setSource(next);
          setErrors({});
        }}
        onSecret={(next) => {
          setSecret(next);
          setErrors({});
        }}
        onKeyInput={() => {
          setErrors({});
          setForgotten(false);
        }}
      />
      {forgotten && failure ? (
        <p className="text-xs text-fg-muted" data-testid="provider-key-forgotten">
          {tForm("keyForgotten")}
        </p>
      ) : null}
    </FormDialog>
  );
}

/** Delete of a provider account, after a confirm that names it: its key goes with it, and its runs stop taking it. */
export function ProviderDeleteDialog({ account, open, onOpenChange }: { account: Account; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("accounts.provider.deleteForm");
  const write = useProviderWrite();
  const failureOf = useProviderFailure();
  const failure = write.error ? failureOf(write.error) : null;
  return (
    <ConfirmAction
      open={open}
      onOpenChange={(next) => {
        if (!next) write.reset();
        onOpenChange(next);
      }}
      title={t("title", { name: account.name })}
      description={t("description")}
      confirmLabel={t("confirm")}
      pendingLabel={t("pending")}
      cancelLabel={t("cancel")}
      pending={write.pending}
      error={failure}
      onConfirm={async () => {
        const done = await write.remove(account.name);
        if (!done.ok) return;
        notify({ tone: "success", text: t("deleted", { name: account.name }), description: t("deletedText") });
        onOpenChange(false);
      }}
      testId="provider-delete-dialog"
    />
  );
}
