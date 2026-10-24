"use client";

import { Terminal } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";

import type { Decision } from "./queries";

/** Take over, for the owner who may open the run's terminal now: the run page's Terminal tab, or `onTakeOver` there. */
export function TakeOverButton({
  decision,
  href,
  onTakeOver,
  className,
}: {
  decision: Decision;
  href: Route;
  onTakeOver?: () => void;
  className?: string;
}) {
  const t = useTranslations("inbox.answer");
  const label = t("takeOverLabel", { run: decision.run_id });
  if (onTakeOver) {
    return (
      <Button type="button" variant="outline" onClick={onTakeOver} aria-label={label} className={className} data-testid="decision-takeover">
        <Terminal aria-hidden="true" />
        {t("takeOver")}
      </Button>
    );
  }
  return (
    <Button asChild variant="outline" className={className}>
      <Link href={href} aria-label={label} data-testid="decision-takeover">
        <Terminal aria-hidden="true" />
        {t("takeOver")}
      </Link>
    </Button>
  );
}
