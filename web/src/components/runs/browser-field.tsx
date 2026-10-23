"use client";

import { queryOptions, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";

import { FieldLabel } from "./field-label";

/** What the field's two values that are not an identity mean: no browser, and a Chromium with no login. */
export const NO_BROWSER = "";
export const BARE_BROWSER = "*";

/** The identities of the project the visitor may use: the key and the answer of the Browser identities page's own query. */
const identitiesOf = (project: string) =>
  queryOptions({
    queryKey: ["projects", project, "browser-identities"] as const,
    queryFn: ({ signal }) => call(browserApi().GET("/v1/projects/{project}/browser-identities", { params: { path: { project } }, signal })),
  });

/**
 * The browser of a dispatch (docs/browser.md): none, a Chromium with no login, or one of the identities the visitor
 * may use (their own and the project's, never an agent's: the profile hands those to its runs). A disabled identity
 * or one that needs a login is listed but cannot be picked. When the identities cannot be read, only the first two
 * are offered, so a dispatch never waits on them.
 */
export function BrowserField({
  project,
  value,
  onChange,
  disabled = false,
}: {
  project: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("runs.browser");
  const id = useId();
  const identities = useQuery(identitiesOf(project));
  const usable = (identities.data ?? []).filter((identity) => identity.owner !== "agent");
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <FieldLabel htmlFor={`${id}-browser`} label={t("label")} info={t("info")} testId="dispatch-browser-info" />
      <NativeSelect
        id={`${id}-browser`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        className="w-full md:[&_select]:h-9"
        data-testid="dispatch-browser"
      >
        <NativeSelectOption value={NO_BROWSER}>{t("none")}</NativeSelectOption>
        <NativeSelectOption value={BARE_BROWSER}>{t("bare")}</NativeSelectOption>
        {usable.map((identity) => (
          <NativeSelectOption key={identity.id} value={identity.name} disabled={identity.status !== "active"}>
            {t("identity", { name: identity.name, owner: identity.owner })}
            {identity.status === "active" ? "" : ` (${t(`status.${identity.status}`)})`}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </div>
  );
}

/** The `browser` and `identities` a dispatch sends for the field's value: nothing for no browser. */
export function browserFields(value: string): { browser?: true; identities?: string[] } {
  if (value === NO_BROWSER) return {};
  if (value === BARE_BROWSER) return { browser: true };
  return { identities: [value] };
}
