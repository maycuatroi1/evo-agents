"use client";

import { GitMerge } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { Button } from "@/components/ui/button";

import type { Designer } from "./use-designer";

/**
 * Someone saved the system since the designer opened it: the save was refused and nothing was lost. Load latest drops
 * this draft for the revision the hub holds; Keep my changes saves this draft over it on the next Save.
 */
export function ConflictBanner({ designer }: { designer: Designer }) {
  const t = useTranslations("systems.conflict");
  const [loading, setLoading] = useState(false);
  const { conflict } = designer;
  if (!conflict) return null;
  return (
    <Banner
      icon={GitMerge}
      tone="attention"
      role="alert"
      title={conflict.revision ? t("title", { revision: conflict.revision }) : t("titleUnknown")}
      testId="system-conflict"
      actions={
        <>
          <Button
            type="button"
            size="sm"
            busy={loading}
            onClick={() => {
              setLoading(true);
              void designer.loadLatest().finally(() => setLoading(false));
            }}
            data-testid="system-conflict-load"
          >
            {t("load")}
          </Button>
          <Button type="button" size="sm" variant="outline" disabled={!conflict.revision || loading} onClick={designer.keepMine} data-testid="system-conflict-keep">
            {t("keep")}
          </Button>
        </>
      }
    >
      {t("text")}
    </Banner>
  );
}
