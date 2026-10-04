"use client";

import { RotateCcw, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect } from "react";

import { StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";

/** A render error inside the shell: the sidebar stays, the page offers a retry and a reference to report. */
export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const t = useTranslations("states");
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <StatePanel
      icon={TriangleAlert}
      tone="danger"
      title={t("unexpectedTitle")}
      description={t("unexpectedDescription")}
      testId="state-unexpected"
    >
      <Button size="lg" onClick={reset}>
        <RotateCcw aria-hidden="true" />
        {t("retry")}
      </Button>
      {error.digest ? (
        <p className="basis-full text-xs text-muted-foreground">
          {t("reference")}: <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground">{error.digest}</code>
        </p>
      ) : null}
    </StatePanel>
  );
}
