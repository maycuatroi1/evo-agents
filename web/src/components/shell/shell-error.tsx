"use client";

import { useTranslations } from "next-intl";

import { BrandMark } from "@/components/brand";
import { ApiErrorState } from "@/components/states/states";
import type { ApiErrorInfo } from "@/lib/api/errors";

/** When the API cannot say who the visitor is, there is no shell to draw: the error alone, with a retry. */
export function ShellError({ error }: { error: ApiErrorInfo }) {
  const t = useTranslations("app");
  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-8 px-4 py-12">
      <div className="flex items-center gap-3">
        <BrandMark />
        <span className="text-lg font-semibold tracking-tight">{t("name")}</span>
      </div>
      <ApiErrorState error={error} onRetry={() => window.location.reload()} />
    </main>
  );
}
