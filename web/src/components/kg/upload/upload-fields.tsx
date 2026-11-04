"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { defaultSource } from "@/lib/kg/upload";

/** A field's words under it, tied to the control by `aria-describedby`. */
export function FieldHint({ id, children }: { id: string; children: ReactNode }) {
  return (
    <p id={id} className="text-xs text-muted-foreground">
      {children}
    </p>
  );
}

/** A field's error under it, read out when it appears. */
export function FieldError({ id, text, testId }: { id: string; text: string | null; testId?: string }) {
  if (!text) return null;
  return (
    <p id={id} role="alert" className="text-xs font-medium text-danger" data-testid={testId}>
      {text}
    </p>
  );
}

/**
 * The source name and the label of an upload. The source is optional: left empty, the hub names it after the archive,
 * the name shown as its placeholder. The label is picked among `levels` only, the labels the hub sink clears.
 */
export function UploadFields({
  file,
  source,
  onSource,
  levels,
  label,
  onLabel,
  labelHint,
  disabled,
}: {
  file: File | null;
  source: string;
  onSource: (value: string) => void;
  levels: readonly string[];
  label: string;
  onLabel: (value: string) => void;
  labelHint: string;
  disabled: boolean;
}) {
  const t = useTranslations("kgUpload.form");
  const ids = useId();
  const shownSource = source.trim() || (file ? defaultSource(file.name) : "");
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <div className="flex min-w-0 flex-col gap-1.5">
        <Label htmlFor={`${ids}-source`}>{t("source")}</Label>
        <Input
          id={`${ids}-source`}
          value={source}
          onChange={(event) => onSource(event.target.value)}
          placeholder={file ? defaultSource(file.name) : undefined}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={200}
          className="font-mono placeholder:font-mono"
          aria-describedby={`${ids}-source-hint`}
          disabled={disabled}
          data-testid="kg-upload-source"
        />
        <FieldHint id={`${ids}-source-hint`}>{shownSource ? t("sourceHint", { source: shownSource }) : t("sourceHintEmpty")}</FieldHint>
      </div>
      <div className="flex min-w-0 flex-col gap-1.5">
        <Label htmlFor={`${ids}-label`}>{t("label")}</Label>
        <NativeSelect
          id={`${ids}-label`}
          value={label}
          onChange={(event) => onLabel(event.target.value)}
          aria-describedby={`${ids}-label-hint`}
          disabled={disabled}
          className="w-full"
          data-testid="kg-upload-label"
        >
          {levels.map((level) => (
            <NativeSelectOption key={level} value={level}>
              {level}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <FieldHint id={`${ids}-label-hint`}>{labelHint}</FieldHint>
      </div>
    </div>
  );
}
