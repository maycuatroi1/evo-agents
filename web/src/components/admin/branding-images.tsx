"use client";

import { ImageUp, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ChangeEvent, useRef, useState } from "react";

import { BrandMark } from "@/components/brand";
import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

import { type ImageAsset, putBrandingImage, type SettingsLayer, writeBranding } from "./settings-data";
import { useSettingsWrite } from "./use-settings-write";

/** evo_agents.hub.server.branding: MAX_IMAGE_BYTES, and the types its first bytes may be. */
export const MAX_IMAGE_BYTES = 256 * 1024;
const TYPES: Record<ImageAsset, string> = {
  logo: "image/png,image/jpeg,image/webp",
  favicon: "image/png,image/jpeg,image/webp,image/x-icon,image/vnd.microsoft.icon,.ico",
};

type Image = { sha256: string; type: string; size: number };

function imageOf(layer: SettingsLayer, asset: ImageAsset): Image | null {
  const held = layer.values[asset] as Image | undefined;
  return held?.sha256 ? held : null;
}

/** The file's bytes as base64, which PUT /v1/admin/branding/{asset} takes. */
function base64Of(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/**
 * The logo, which takes the mark's place beside the product's name, and the favicon, each stored in the hub's blob
 * store. Uploading one writes the next version of the branding; removing it brings back the web's own. The hub checks
 * the type by the file's first bytes and refuses anything over 256 KiB; the page says so before sending.
 */
export function BrandingImages({ layer }: { layer: SettingsLayer }) {
  const t = useTranslations("admin.branding.images");
  return (
    <Card className="min-w-0" data-testid="branding-images">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description", { kib: MAX_IMAGE_BYTES / 1024 })}</CardDescription>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col divide-y">
        {(["logo", "favicon"] as const).map((asset) => (
          <ImageRow key={asset} asset={asset} layer={layer} />
        ))}
      </CardContent>
    </Card>
  );
}

function ImageRow({ asset, layer }: { asset: ImageAsset; layer: SettingsLayer }) {
  const t = useTranslations("admin.branding.images");
  const failure = useWriteFailure();
  const input = useRef<HTMLInputElement>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const upload = useSettingsWrite(putBrandingImage, { refresh: true });
  const remove = useSettingsWrite(writeBranding, { refresh: true });
  const image = imageOf(layer, asset);
  const pending = upload.isPending || remove.isPending;
  const failed = upload.isError ? upload.error : remove.isError ? remove.error : null;
  const error = failed ? failure(failed, { 409: t("conflict") }) : null;

  const picked = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = ""; // the same file may be picked again after a failure
    if (!file) return;
    upload.reset();
    remove.reset();
    if (file.size > MAX_IMAGE_BYTES) {
      setProblem(t("tooBig", { kib: MAX_IMAGE_BYTES / 1024 }));
      return;
    }
    setProblem(null);
    try {
      await upload.mutateAsync({ asset, data: await base64Of(file), ifVersion: layer.version });
    } catch {
      return; // said under the row, from the mutation's state
    }
    notify({ tone: "success", text: t(`${asset}Saved`) });
  };

  const drop = async () => {
    if (pending) return;
    setProblem(null);
    try {
      await remove.mutateAsync({ values: {}, unset: [asset], if_version: layer.version });
    } catch {
      return;
    }
    notify({ tone: "success", text: t(`${asset}Removed`) });
  };

  const url = image ? `/v1/branding/${asset}?v=${image.sha256.slice(0, 16)}` : null;
  return (
    <div className="flex flex-col gap-3 py-4 first:pt-0 last:pb-0" data-testid={`branding-${asset}`}>
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-md border bg-muted">
          {url ? (
            // eslint-disable-next-line @next/next/no-img-element -- an image the hub API serves
            <img src={url} alt={t(`${asset}Alt`)} className="size-9 object-contain" data-testid={`branding-${asset}-image`} />
          ) : (
            <BrandMark className="size-8" />
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <p className="text-sm font-medium">{t(asset)}</p>
          <p className="text-[13px] leading-[18px] text-muted-foreground">
            {image ? t("held", { kib: Math.max(1, Math.round(image.size / 1024)), type: image.type }) : t(`${asset}Default`)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <input
            ref={input}
            type="file"
            accept={TYPES[asset]}
            className="sr-only"
            tabIndex={-1}
            aria-hidden="true"
            onChange={(event) => void picked(event)}
            data-testid={`branding-${asset}-file`}
          />
          <Button
            type="button"
            variant="outline"
            busy={upload.isPending}
            aria-disabled={pending || undefined}
            onClick={() => {
              if (!pending) input.current?.click();
            }}
            data-testid={`branding-${asset}-upload`}
          >
            <ImageUp aria-hidden="true" />
            {t(image ? `${asset}Replace` : `${asset}Upload`)}
          </Button>
          {image ? (
            <Button
              type="button"
              variant="quiet-danger"
              busy={remove.isPending}
              aria-disabled={pending || undefined}
              onClick={() => void drop()}
              data-testid={`branding-${asset}-remove`}
            >
              <Trash2 aria-hidden="true" />
              {t(`${asset}Remove`)}
            </Button>
          ) : null}
        </div>
      </div>
      {problem ? (
        <p role="alert" className="text-xs font-medium text-danger">
          {problem}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
    </div>
  );
}
