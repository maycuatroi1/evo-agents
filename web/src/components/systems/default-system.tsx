"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Star, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { Banner } from "@/components/feedback/banner";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { setDefaultSystem } from "@/lib/system-ops-queries";
import { systemKeys } from "@/lib/systems-queries";

/**
 * The default system of a project (docs/hub.md, Agent systems): the one whose Orchestrator handles the project's
 * operations events. The Default tag is information, not a state, so it is a plain outline badge; only a project
 * with no default system gets a banner, since then nothing handles them.
 */
export function DefaultTag() {
  const t = useTranslations("systems.default");
  return (
    <Badge variant="outline" className="shrink-0 gap-1" data-testid="system-default">
      <Star className="size-3" aria-hidden="true" />
      {t("tag")}
    </Badge>
  );
}

/** Make a system the project's default, for an admin; every read of the project's systems again after. */
export function useSetDefault(project: string) {
  const t = useTranslations("systems.default");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => setDefaultSystem(browserApi(), project, slug),
    onSuccess: (written) => {
      notify({ tone: "success", text: t("setDone", { name: written.name ?? written.slug ?? "" }), description: t("setDoneText") });
      return queryClient.invalidateQueries({ queryKey: systemKeys.all(project) });
    },
    onError: (error) => notifyFailure(t("setFailed"), failure(error, { 403: t("forbidden") })),
  });
}

/** Set as default on a system's row; the row's name stays its label for a screen reader. */
export function SetDefaultButton({ name, busy, onClick }: { name: string; busy: boolean; onClick: () => void }) {
  const t = useTranslations("systems.default");
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      busy={busy}
      onClick={onClick}
      aria-label={t("setFor", { name })}
      className="relative z-10 max-md:min-h-11"
      data-testid="system-set-default"
    >
      <Star aria-hidden="true" />
      {t("set")}
    </Button>
  );
}

/** A project with systems and no default: nothing handles its operations events until an admin picks one. */
export function NoDefaultBanner({ admin }: { admin: boolean }) {
  const t = useTranslations("systems.default");
  return (
    <Banner icon={TriangleAlert} tone="attention" title={t("noneTitle")} testId="systems-no-default">
      {admin ? t("noneAdmin") : t("noneReader")}
    </Banner>
  );
}
