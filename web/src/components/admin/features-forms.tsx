"use client";

import { Save, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Segmented } from "@/components/data/segmented";
import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { type FeatureId, type Features } from "@/lib/hub-settings";

import { type SettingsLayer, type SettingsWrite, writeFeatures } from "./settings-data";
import { useSettingsWrite } from "./use-settings-write";

/** What a project's layer does with a feature: nothing (it follows the hub's), or turns it on or off. */
export type Choice = "hub" | "on" | "off";
type Choices = Partial<Record<FeatureId, Choice>>;

function choicesOf(layer: SettingsLayer, ids: readonly FeatureId[]): Record<FeatureId, Choice> {
  const values = layer.values as Partial<Record<FeatureId, boolean>>;
  const pick = (id: FeatureId): Choice => (values[id] === undefined ? "hub" : values[id] ? "on" : "off");
  return Object.fromEntries(ids.map((id) => [id, pick(id)])) as Record<FeatureId, Choice>;
}

/**
 * What a save of a layer sends: each feature whose choice changed, set to true or false, or unset to follow the layer
 * below (for the hub's own layer, on). Only those that changed, at the layer's version.
 */
export function featuresWrite(layer: SettingsLayer, ids: readonly FeatureId[], edits: Choices): SettingsWrite {
  const held = choicesOf(layer, ids);
  const body: SettingsWrite = { values: {}, unset: [], if_version: layer.version };
  for (const id of ids) {
    const choice = edits[id];
    if (choice === undefined || choice === held[id]) continue;
    if (choice === "hub") body.unset!.push(id);
    else body.values![id] = choice === "on";
  }
  return body;
}

type Props = {
  layer: SettingsLayer;
  project: string | null;
  ids: readonly FeatureId[];
  /** What the hub's own layer makes of each feature: a project row says what following it means. */
  hub: Features | undefined;
};

/**
 * The features one layer sets, as a form: for the hub's own layer a switch per feature (off sets it off, on unsets
 * it, since every feature is on by default); for a project's layer a choice of following the hub, on or off, for each
 * feature a project may set. Saving writes the next version of the layer, and the sidebar follows at once.
 */
export function FeatureLayerForm({ layer, project, ids, hub }: Props) {
  const t = useTranslations("admin.features");
  const formId = useId();
  const failure = useWriteFailure();
  const write = useSettingsWrite(writeFeatures);
  const [edits, setEdits] = useState<Choices>({});
  const held = choicesOf(layer, ids);
  const body = featuresWrite(layer, ids, edits);
  const dirty = Boolean(body.unset?.length || Object.keys(body.values ?? {}).length);
  const pending = write.isPending;
  const where = project ?? "hub";

  const choose = (id: FeatureId, choice: Choice) => {
    setEdits({ ...edits, [id]: choice });
    if (write.isError) write.reset();
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || !dirty) return;
    try {
      await write.mutateAsync({ project, body });
    } catch {
      return; // said above the buttons, from the mutation's state
    }
    setEdits({});
    notify({ tone: "success", text: project ? t("savedProject", { project }) : t("savedHub") });
  };
  const error = write.isError ? failure(write.error, { 409: t("conflict"), 404: t("projectGone") }) : null;
  const state = (id: FeatureId) => hub?.features.find((feature) => feature.id === id);
  const hubOn = (id: FeatureId) => state(id)?.enabled ?? true;

  return (
    <form
      onSubmit={(event) => void submit(event)}
      noValidate
      aria-busy={pending || undefined}
      className="flex flex-col gap-4"
      data-testid={`features-form-${where}`}
    >
      <ul className="flex flex-col divide-y">
        {ids.map((id) => {
          const choice = edits[id] ?? held[id];
          return (
            <li
              key={id}
              className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3 first:pt-0"
              data-testid={`feature-${where}-${id}`}
              data-choice={choice}
            >
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                  <span id={`${formId}-${id}`}>{t(`names.${id}`)}</span>
                  {project === null ? (
                    <Badge variant="outline">{t(state(id)?.layers.includes("project") ? "scopeProject" : "scopeHub")}</Badge>
                  ) : null}
                </p>
                <p className="text-[13px] leading-[18px] text-muted-foreground">{t(`descriptions.${id}`)}</p>
              </div>
              {project === null ? (
                <div className="flex items-center gap-2">
                  <span className="text-[13px] text-muted-foreground" aria-hidden="true">
                    {choice === "off" ? t("off") : t("on")}
                  </span>
                  <Switch
                    checked={choice !== "off"}
                    onCheckedChange={(checked) => choose(id, checked ? "hub" : "off")}
                    aria-labelledby={`${formId}-${id}`}
                    data-testid={`feature-hub-${id}-switch`}
                  />
                </div>
              ) : (
                <Segmented<Choice>
                  label={t("choiceLabel", { feature: t(`names.${id}`), project })}
                  value={choice}
                  onChange={(next) => choose(id, next)}
                  options={[
                    { value: "hub", label: t(hubOn(id) ? "followOn" : "followOff"), testId: `feature-${where}-${id}-hub` },
                    { value: "on", label: t("on"), testId: `feature-${where}-${id}-on` },
                    { value: "off", label: t("off"), testId: `feature-${where}-${id}-off` },
                  ]}
                />
              )}
            </li>
          );
        })}
      </ul>
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <div className="flex flex-wrap justify-end gap-2">
        {dirty ? (
          <Button
            type="button"
            variant="outline"
            aria-disabled={pending || undefined}
            onClick={() => {
              if (pending) return;
              setEdits({});
              write.reset();
            }}
            data-testid={`features-discard-${where}`}
          >
            <Undo2 aria-hidden="true" />
            {t("discard")}
          </Button>
        ) : null}
        <Button type="submit" busy={pending} disabled={!dirty} data-testid={`features-save-${where}`}>
          <Save aria-hidden="true" />
          {pending ? t("saving") : t("save")}
        </Button>
      </div>
    </form>
  );
}
