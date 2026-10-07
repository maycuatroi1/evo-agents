"use client";

import { CircleCheck, Download } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { type ApiErrorInfo, errorKind, toInfo } from "@/lib/api/errors";
import type { ApiSource } from "@/lib/queries";

import { bundleFileName } from "./format";
import { type BlobStoreState, bundleTicket, type SkillPlace } from "./queries";

/** Sends the browser to a URL; the page stays, since the blob store answers with an attachment. */
export type Navigate = (url: string) => void;

const navigateTo: Navigate = (url) => window.location.assign(url);

type Props = {
  place: SkillPlace;
  name: string;
  version: number;
  blobStore: BlobStoreState;
  variant?: "default" | "outline";
  compact?: boolean;
  /** For tests: the API and the navigation. */
  api?: ApiSource;
  navigate?: Navigate;
};

type State = { status: "idle" } | { status: "pending" } | { status: "started" } | { status: "failed"; error: ApiErrorInfo };

/**
 * Downloads one version's bundle: asks the API for a presigned GET, then navigates the browser to it. Nothing is
 * fetched by script, so the bucket needs no CORS; the hub signs the URL as an attachment named
 * `<name>-v<version>.tar.gz`, so the page stays where it is. Without a blob store on the hub the button is off and
 * says why; a refusal or an outage shows under the button with what to do.
 */
export function DownloadBundle({
  place,
  name,
  version,
  blobStore,
  variant = "default",
  compact = false,
  api = browserApi,
  navigate = navigateTo,
}: Props) {
  const t = useTranslations("skills.download");
  const [state, setState] = useState<State>({ status: "idle" });
  const unconfigured = blobStore === "unconfigured";
  const file = bundleFileName(name, version);

  const start = async () => {
    setState({ status: "pending" });
    try {
      const ticket = await bundleTicket(api, place, name, version);
      navigate(ticket.url);
      setState({ status: "started" });
    } catch (error) {
      setState({ status: "failed", error: toInfo(error) });
    }
  };

  let message: ReactNode = null;
  if (state.status === "started") {
    message = (
      <span className="inline-flex items-center gap-1.5 text-muted-foreground">
        <CircleCheck className="size-3.5 shrink-0" aria-hidden="true" />
        {t("started", { file })}
      </span>
    );
  } else if (state.status === "failed") {
    const { error } = state;
    const kind = errorKind(error.status);
    const text =
      kind === "forbidden"
        ? t("forbidden")
        : kind === "not_found"
          ? t("notFound")
          : error.status === 503
            ? t("unavailable")
            : kind === "network"
              ? t("network")
              : t("failed", { status: error.status, requestId: error.requestId ?? "-" });
    message = <span className="text-danger">{text}</span>;
  }

  const label = compact ? t("short") : t("button", { version });
  return (
    <div className="flex flex-col items-start gap-1.5">
      <Button
        type="button"
        variant={variant}
        size={compact ? "sm" : "lg"}
        onClick={() => void start()}
        disabled={unconfigured}
        busy={state.status === "pending"}
        aria-label={compact ? t("aria", { name, version }) : undefined}
        title={unconfigured ? t("unconfigured") : undefined}
        data-testid={`download-v${version}`}
      >
        <Download aria-hidden="true" />
        {state.status === "pending" ? t("pending") : label}
      </Button>
      <p aria-live="polite" className="min-h-0 text-xs empty:hidden" data-testid={`download-status-v${version}`}>
        {message}
      </p>
    </div>
  );
}

