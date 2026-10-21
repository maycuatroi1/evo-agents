"use client";

import { Save, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { BRAND_TOKENS, type BrandToken, THEMES, type Theme } from "@/lib/hub-settings";

import { type Colors, checkColors, colorsOf, HEX, MAX_NAME, nameProblem, palette, storedColors } from "./branding-model";
import { ColorField, ThemeChecks, ThemePreview } from "./branding-parts";
import { type SettingsLayer, type SettingsWrite, writeBranding } from "./settings-data";
import { useSettingsWrite } from "./use-settings-write";

type Form = { name: string; colors: Colors };

function formOf(layer: SettingsLayer): Form {
  return { name: typeof layer.values.name === "string" ? layer.values.name : "", colors: colorsOf(layer.values) };
}

/** What a save sends: the fields that differ from the layer, the name or the colours set or unset, at its version. */
export function brandingWrite(layer: SettingsLayer, form: Form): SettingsWrite {
  const held = formOf(layer);
  const body: SettingsWrite = { values: {}, unset: [], if_version: layer.version };
  const name = form.name.trim();
  if (name !== held.name) {
    if (name) body.values!.name = name;
    else body.unset!.push("name");
  }
  const colors = storedColors(form.colors);
  if (JSON.stringify(colors) !== JSON.stringify(storedColors(held.colors))) {
    if (Object.keys(colors).length) body.values!.colors = colors;
    else body.unset!.push("colors");
  }
  return body;
}

/**
 * The product's name and the colours of the four tokens a hub may set, for each theme, with the pairs web/DESIGN.md
 * measures and a preview beside the fields of each theme, both following what is typed. A save is held back while a
 * pair falls under its ratio or a field does not fit, since the hub would refuse it; once the hub answers, the page is
 * rendered again with the new colours, title and favicon.
 */
export function BrandingForm({ layer, defaultName }: { layer: SettingsLayer; defaultName: string }) {
  const t = useTranslations("admin.branding");
  const ids = useId();
  const failure = useWriteFailure();
  const write = useSettingsWrite(writeBranding, { refresh: true });
  const [edits, setEdits] = useState<Form | null>(null);
  const [tried, setTried] = useState(false);
  const form = edits ?? formOf(layer);
  const pending = write.isPending;

  const nameError = form.name && nameProblem(form.name) ? t("nameInvalid", { max: MAX_NAME }) : undefined;
  const colorError = (theme: Theme, token: BrandToken) => {
    const value = form.colors[theme][token] ?? "";
    return value && !HEX.test(value) ? t("colorInvalid") : undefined;
  };
  const results = checkColors(form.colors);
  const failing = results.filter((result) => !result.passes).length;
  const invalid = Boolean(nameError) || THEMES.some((theme) => BRAND_TOKENS.some((token) => colorError(theme, token)));
  const body = brandingWrite(layer, form);
  const dirty = Boolean(body.unset?.length || Object.keys(body.values ?? {}).length);

  const change = (next: Form) => {
    setEdits(next);
    if (write.isError) write.reset();
  };
  const setColor = (theme: Theme, token: BrandToken, value: string) =>
    change({ ...form, colors: { ...form.colors, [theme]: { ...form.colors[theme], [token]: value } } });

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (pending || !dirty || invalid || failing) return;
    try {
      await write.mutateAsync(body);
    } catch {
      return; // said above the buttons, from the mutation's state
    }
    setEdits(null);
    setTried(false);
    notify({ tone: "success", text: t("saved"), description: t("savedText") });
  };

  const error = write.isError ? failure(write.error, { 409: t("conflict") }) : null;
  const shownName = form.name.trim() || defaultName;
  return (
    <Card className="min-w-0" data-testid="branding-form">
      <CardHeader>
        <CardTitle>
          <h2 id={`${ids}-title`}>{t("formTitle")}</h2>
        </CardTitle>
        <CardDescription>{t("formDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">
        <form
          onSubmit={(event) => void submit(event)}
          noValidate
          aria-labelledby={`${ids}-title`}
          aria-busy={pending || undefined}
          className="flex flex-col gap-6"
        >
          <div className="flex max-w-md min-w-0 flex-col gap-1.5">
            <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
            <Input
              id={`${ids}-name`}
              value={form.name}
              onChange={(event) => change({ ...form, name: event.target.value })}
              placeholder={defaultName}
              maxLength={MAX_NAME + 20}
              autoComplete="off"
              className="h-9"
              aria-invalid={nameError ? true : undefined}
              aria-describedby={`${ids}-name-hint${nameError ? ` ${ids}-name-error` : ""}`}
              data-testid="branding-name"
            />
            <p id={`${ids}-name-hint`} className="text-xs text-muted-foreground">
              {t("nameHint", { name: defaultName })}
            </p>
            {nameError ? (
              <p id={`${ids}-name-error`} className="text-xs font-medium text-danger">
                {nameError}
              </p>
            ) : null}
          </div>
          <div className="grid min-w-0 gap-6 lg:grid-cols-2">
            {THEMES.map((theme) => (
              <fieldset key={theme} className="flex min-w-0 flex-col gap-4" data-testid={`branding-theme-${theme}`}>
                <legend className="mb-3 text-[15px] leading-[22px] font-semibold">{t(`themes.${theme}`)}</legend>
                <div className="grid min-w-0 gap-4 sm:grid-cols-2">
                  {BRAND_TOKENS.map((token) => (
                    <ColorField
                      key={token}
                      id={`${ids}-${theme}-${token}`}
                      theme={theme}
                      token={token}
                      value={form.colors[theme][token] ?? ""}
                      error={colorError(theme, token)}
                      onChange={(value) => setColor(theme, token, value)}
                    />
                  ))}
                </div>
                <ThemePreview theme={theme} palette={palette(theme, form.colors)} name={shownName} />
                <ThemeChecks theme={theme} results={results.filter((result) => result.theme === theme)} />
              </fieldset>
            ))}
          </div>
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          {tried && (failing > 0 || invalid) ? (
            <p role="alert" className="text-sm font-medium text-danger" data-testid="branding-blocked">
              {failing > 0 ? t("blockedContrast", { count: failing }) : t("blockedInvalid")}
            </p>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            {edits !== null ? (
              <Button
                type="button"
                variant="outline"
                aria-disabled={pending || undefined}
                onClick={() => {
                  if (pending) return;
                  setEdits(null);
                  setTried(false);
                  write.reset();
                }}
                data-testid="branding-discard"
              >
                <Undo2 aria-hidden="true" />
                {t("discard")}
              </Button>
            ) : null}
            <Button type="submit" busy={pending} disabled={!dirty} data-testid="branding-save">
              <Save aria-hidden="true" />
              {pending ? t("saving") : t("save")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
