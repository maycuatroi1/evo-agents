"use client";

import { Network } from "lucide-react";
import { useTranslations } from "next-intl";

import { EmptyState } from "@/components/states/states";

/** No build of the project succeeded yet: what to run so the graph exists. */
export function NoGraph() {
  const t = useTranslations("kg.builds");
  return (
    <EmptyState
      icon={Network}
      title={t("noGraphTitle")}
      description={t.rich("noGraphDescription", { code: (chunks) => <code className="font-mono">{chunks}</code> })}
    />
  );
}
