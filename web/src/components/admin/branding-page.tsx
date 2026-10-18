"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { BrandingForm } from "./branding-form";
import { BrandingImages } from "./branding-images";
import { SettingsHistory } from "./settings-history";
import { brandingLayerQuery, brandingVersionsQuery, rollbackBranding, type SettingsLayer } from "./settings-data";
import { useSettingsWrite } from "./use-settings-write";

const FIELDS = ["name", "colors", "logo", "favicon"] as const;

/** The versions of the branding with a rollback to each older one. */
function BrandingHistory() {
  const t = useTranslations("admin.branding");
  const tHistory = useTranslations("admin.settings.history");
  const failure = useWriteFailure();
  const versions = useHubQuery(brandingVersionsQuery(browserApi));
  const rollback = useSettingsWrite(rollbackBranding, { refresh: true });
  const [pending, setPending] = useState<number | null>(null);
  const describe = (layer: SettingsLayer) => {
    const set = FIELDS.filter((field) => layer.values[field] !== undefined).map((field) => t(`fields.${field}`));
    return set.length ? tHistory("sets", { fields: set.join(", ") }) : tHistory("setsNothing");
  };
  const roll = async (version: number, current: number) => {
    setPending(version);
    try {
      await rollback.mutateAsync({ version, if_version: current });
      notify({ tone: "success", text: tHistory("rolledBackTo", { version }) });
    } catch (error) {
      notifyFailure(tHistory("rollbackFailed", { version }), failure(error, { 409: t("conflict") }));
    } finally {
      setPending(null);
    }
  };
  return (
    <QueryView state={versions} loading={<PageSkeleton />}>
      {(list) => (
        <SettingsHistory versions={list} describe={describe} onRollback={(v, c) => void roll(v, c)} pending={pending} testId="branding-history" />
      )}
    </QueryView>
  );
}

/**
 * /admin/branding, for hub admins: the product's name and the colours of the four tokens a hub may set, checked
 * against the contrast web/DESIGN.md asks for in both themes; the logo and the favicon; and every version, with a
 * rollback. With nothing set the web keeps its own name, mark, favicon and colours. Anyone else gets the API's 403.
 */
export function AdminBrandingPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.branding");
  const tApp = useTranslations("app");
  const state = useHubQuery(brandingLayerQuery(browserApi), initialError);
  return (
    <>
      <PageHeader
        title={t("title")}
        tags={
          state.status === "success" ? (
            <Badge variant="secondary" className="tabular-nums" data-testid="branding-version">
              {state.data.version ? t("version", { version: state.data.version }) : t("unset")}
            </Badge>
          ) : null
        }
      />
      <QueryView state={state} loading={<PageSkeleton />}>
        {(layer) => (
          <div className="grid min-w-0 gap-6 2xl:grid-cols-[minmax(0,3fr)_minmax(0,1fr)]">
            <div className="flex min-w-0 flex-col gap-6">
              <BrandingForm layer={layer} defaultName={tApp("name")} />
              <BrandingImages layer={layer} />
            </div>
            <div className="flex min-w-0 flex-col gap-6">
              <BrandingHistory />
            </div>
          </div>
        )}
      </QueryView>
    </>
  );
}
