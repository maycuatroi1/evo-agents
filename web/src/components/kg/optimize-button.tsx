"use client";

import { Gauge } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { csrfHeaders } from "@/lib/api/csrf";

/**
 * Asks the hub for a KG optimize run of the project now (POST /v1/kg/{project}/optimize, the writer role): 201 for a
 * new run, 200 for the run in flight. The run itself goes on in the hub's job; its report comes as a notification.
 */
export function OptimizeButton({ project }: { project: string }) {
  const t = useTranslations("kg.optimize");
  const [busy, setBusy] = useState(false);
  async function start() {
    setBusy(true);
    try {
      const headers = await csrfHeaders(browserApi());
      const response = await fetch(`/v1/kg/${encodeURIComponent(project)}/optimize`, {
        method: "POST",
        credentials: "same-origin",
        headers: { ...headers, accept: "application/json", "content-type": "application/json" },
        body: "{}",
      });
      const body = (await response.json().catch(() => ({}))) as { id?: number; state?: string; message?: string };
      if (response.status === 201) notify({ tone: "success", text: t("started", { id: body.id ?? 0 }) });
      else if (response.ok) notify({ tone: "info", text: t("inFlight", { id: body.id ?? 0, state: body.state ?? "" }) });
      else notify({ tone: "error", text: t("failed"), detail: body.message ?? null });
    } catch {
      notify({ tone: "error", text: t("failed") });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Button variant="outline" size="sm" onClick={start} disabled={busy} data-testid="kg-optimize">
      <Gauge aria-hidden="true" />
      {t("label")}
    </Button>
  );
}
