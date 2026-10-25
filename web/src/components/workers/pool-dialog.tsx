"use client";

import { Boxes } from "lucide-react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { isApiError } from "@/lib/api/errors";

import { FormError } from "./form-error";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import { PoolFields } from "./pool-fields";
import { emptyPoolForm, POOL_FIELDS, type PoolErrors, type PoolField, type PoolForm, readPoolForm } from "./pool-model";
import { createPool, MAX_SLOTS, poolHref, type PoolRequest } from "./queries";

/** The first field with a problem gets the focus; a project is its first checkbox. */
export function focusFirst(ids: string, errors: PoolErrors) {
  const first = POOL_FIELDS.find((field) => errors[field]);
  if (first) document.getElementById(first === "projects" ? `${ids}-project-0` : `${ids}-${first}`)?.focus();
}

/**
 * Making a pool: its name, projects and what every node gets. Once the hub answers, the visitor lands on the new
 * pool's page, where a join token adds its first node. A name the visitor uses for another pool already is said at the
 * name. The content mounts each time the dialog opens, so a new opening starts empty.
 */
export function PoolDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const write = useWorkerWrite((api, body: PoolRequest) => createPool(api, body));
  const pending = write.isPending;
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-2xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="pool-dialog"
      >
        <PoolCreate write={write} onClose={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

type Write = ReturnType<typeof useWorkerWrite<PoolRequest, Awaited<ReturnType<typeof createPool>>>>;

function PoolCreate({ write, onClose }: { write: Write; onClose: () => void }) {
  const t = useTranslations("workers.pools.create");
  const ids = useId();
  const router = useRouter();
  const failure = useWorkerFailure();
  const [form, setForm] = useState<PoolForm>(emptyPoolForm);
  const [errors, setErrors] = useState<PoolErrors>({});
  const pending = write.isPending;

  const set = <F extends PoolField>(field: F, value: PoolForm[F]) => {
    setForm((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: undefined }));
    if (write.isError) write.reset();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const read = readPoolForm(form, MAX_SLOTS);
    if (read.errors) {
      setErrors(read.errors);
      focusFirst(ids, read.errors);
      return;
    }
    let made;
    try {
      made = await write.mutateAsync(read.pool);
    } catch (error) {
      if (isApiError(error) && error.status === 409) {
        write.reset();
        setErrors({ name: { code: "nameTaken" } });
        document.getElementById(`${ids}-name`)?.focus();
      }
      return;
    }
    onClose();
    notify({ tone: "success", text: t("created", { name: made.name }), description: t("createdText") });
    router.push(poolHref(made.name) as Route);
  };

  const error = write.isError ? failure(write.error) : null;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate aria-busy={pending || undefined} className="flex flex-col gap-5">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
          <Boxes className="size-5 text-muted-foreground" aria-hidden="true" />
          {t("title")}
        </DialogTitle>
        <DialogDescription>{t("description")}</DialogDescription>
      </DialogHeader>
      <fieldset disabled={pending} className="min-w-0">
        <PoolFields ids={ids} form={form} errors={errors} set={set} naming />
      </fieldset>
      {error ? <FormError text={error.text} detail={error.detail} requestId={error.requestId} testId="pool-dialog-error" /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button
            type="button"
            variant="outline"
            size="lg"
            aria-disabled={pending || undefined}
            className="aria-disabled:opacity-50"
            onClick={(event) => pending && event.preventDefault()}
          >
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={pending} data-testid="pool-create">
          {pending ? t("creating") : t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}
