"use client";

import { FilePen, FilePlus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";

import { AuthorRunDialog } from "./author-run-dialog";
import { useAuthorRunToast, useCanDispatch } from "./hooks";

/**
 * The plan pages' side of author runs: New plan on the project's Plans page and Revise with agent on a plan's page,
 * each for a writer of the project (whoami's grants; the API decides again), each opening the author run dialog. The
 * toast after a dispatch links to the run's chat.
 */

export function NewPlanButton({
  project,
  variant = "default",
  testId = "new-plan",
}: {
  project: string;
  /** outline where another button is the view's primary one. */
  variant?: "default" | "outline";
  testId?: string;
}) {
  const t = useTranslations("runs.author");
  const canDispatch = useCanDispatch(project);
  const toast = useAuthorRunToast();
  const [open, setOpen] = useState(false);
  if (!canDispatch) return null;
  return (
    <>
      <Button type="button" variant={variant} className="shrink-0" onClick={() => setOpen(true)} data-testid={testId}>
        <FilePlus aria-hidden="true" />
        {t("newPlan")}
      </Button>
      <AuthorRunDialog project={project} planId={null} open={open} onOpenChange={setOpen} onDispatched={toast} />
    </>
  );
}

export function ReviseWithAgentButton({ project, planId }: { project: string; planId: string }) {
  const t = useTranslations("runs.author");
  const canDispatch = useCanDispatch(project);
  const toast = useAuthorRunToast();
  const [open, setOpen] = useState(false);
  if (!canDispatch) return null;
  return (
    <>
      <Button type="button" variant="outline" className="shrink-0" onClick={() => setOpen(true)} data-testid="revise-with-agent">
        <FilePen aria-hidden="true" />
        {t("revise")}
      </Button>
      <AuthorRunDialog project={project} planId={planId} open={open} onOpenChange={setOpen} onDispatched={toast} />
    </>
  );
}
