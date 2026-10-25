"use client";

import { Save, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

import { FormError } from "./form-error";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import { focusFirst } from "./pool-dialog";
import { PoolFields } from "./pool-fields";
import { type PoolErrors, type PoolField, type PoolForm, poolChanges, poolForm, readPoolForm } from "./pool-model";
import { MAX_SLOTS, type Pool, type PoolUpdate, updatePool } from "./queries";

/**
 * A pool's settings, saved with PATCH /v1/worker-pools/{name}: its nodes take them at their next heartbeat. The fields
 * show what the hub holds until the owner types; a save sends only the fields that differ, and what the hub refuses
 * stays at the form until the next try.
 */
export function PoolSettings({ pool }: { pool: Pool }) {
  const t = useTranslations("workers.pools.settings");
  const ids = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, body: PoolUpdate) => updatePool(api, pool.name, body));
  const [edits, setEdits] = useState<PoolForm | null>(null);
  const [errors, setErrors] = useState<PoolErrors>({});
  const pending = write.isPending;
  const form = edits ?? poolForm(pool);

  const set = <F extends PoolField>(field: F, value: PoolForm[F]) => {
    setEdits({ ...form, [field]: value });
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
    const body = poolChanges(pool, read.pool);
    if (!body) {
      setEdits(null); // what was typed is what the hub holds already
      return;
    }
    try {
      await write.mutateAsync(body);
    } catch {
      return; // said above the buttons, from the mutation's state
    }
    setEdits(null);
    notify({ tone: "success", text: t("saved", { name: pool.name }), description: t("savedText") });
  };

  const error = write.isError ? failure(write.error) : null;
  const dirty = edits !== null && JSON.stringify(edits) !== JSON.stringify(poolForm(pool));

  return (
    <Card className="min-w-0" data-testid="pool-settings">
      <CardHeader>
        <CardTitle>
          <h2 id={`${ids}-title`}>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">
        <form
          onSubmit={(event) => void submit(event)}
          noValidate
          aria-labelledby={`${ids}-title`}
          aria-busy={pending || undefined}
          className="flex flex-col gap-5"
        >
          <PoolFields ids={ids} form={form} errors={errors} set={set} naming={false} kept={pool.projects} />
          {error ? <FormError text={error.text} detail={error.detail} requestId={error.requestId} testId="pool-settings-error" /> : null}
          <div className="flex flex-wrap justify-end gap-2">
            {dirty ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  if (pending) return;
                  setEdits(null);
                  setErrors({});
                  write.reset();
                }}
                aria-disabled={pending || undefined}
                data-testid="pool-settings-discard"
              >
                <Undo2 aria-hidden="true" />
                {t("discard")}
              </Button>
            ) : null}
            <Button type="submit" busy={pending} disabled={!dirty} data-testid="pool-settings-save">
              <Save aria-hidden="true" />
              {pending ? t("saving") : t("save")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
