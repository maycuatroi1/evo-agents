"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { type ApiErrorInfo, toInfo } from "@/lib/api/errors";
import { hashFile } from "@/lib/kg/sha256";
import {
  completeUpload,
  defaultSource,
  type KgUpload,
  type KgUploadStarted,
  partRanges,
  sendMissingParts,
  startUpload,
  uploadHeaders,
} from "@/lib/kg/upload";
import { uploadKeys } from "@/lib/kg/upload-queries";
import { forgetUpload, pendingFor, rememberUpload } from "@/lib/kg/upload-resume";

/**
 * One archive's upload from this page, as a state the page draws: the file hashed (skipped when this browser started
 * the same file before), the upload started or resumed, the parts the store lacks sent, then joined. The upload goes
 * on while the page stays open; leaving it asks first. A reload loses only the bytes in flight: the same file chosen
 * again resumes (`upload-resume`).
 */
export type UploadPhase =
  | { phase: "idle" }
  | { phase: "hashing"; file: File; read: number }
  | { phase: "starting"; file: File }
  | { phase: "sending"; file: File; started: KgUploadStarted; sent: Record<number, number> }
  | { phase: "completing"; file: File; started: KgUploadStarted }
  | { phase: "done"; file: File; upload: KgUpload }
  | { phase: "failed"; file: File; error: ApiErrorInfo }
  | { phase: "cancelled"; file: File };

export type UploadOptions = { project: string; source: string; label: string | null; newProject?: boolean };

export function isBusy(state: UploadPhase): boolean {
  return state.phase === "hashing" || state.phase === "starting" || state.phase === "sending" || state.phase === "completing";
}

/** The bytes the store holds or that were sent so far, of the archive's size. */
export function bytesSent(sent: Record<number, number>): number {
  return Object.values(sent).reduce((total, bytes) => total + bytes, 0);
}

function aborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export type UploadHooks = {
  /** The hub took the upload (and, for a new project, created it): its parts go next. */
  onStarted?: (started: KgUploadStarted, options: UploadOptions) => void;
  onDone?: (upload: KgUpload, options: UploadOptions) => void;
};

export function useUpload({ onStarted, onDone }: UploadHooks = {}) {
  const [state, setState] = useState<UploadPhase>({ phase: "idle" });
  const controller = useRef<AbortController | null>(null);
  const queryClient = useQueryClient();

  const start = useCallback(
    async (file: File, options: UploadOptions) => {
      controller.current?.abort();
      const own = new AbortController();
      controller.current = own;
      const { project } = options;
      const source = options.source.trim() || defaultSource(file.name);
      try {
        const known = options.newProject ? null : pendingFor(project, file);
        let sha256 = known?.sha256;
        if (!sha256) {
          setState({ phase: "hashing", file, read: 0 });
          sha256 = await hashFile(file, (read) => setState({ phase: "hashing", file, read }), own.signal);
        }
        setState({ phase: "starting", file });
        const headers = await uploadHeaders();
        const ask = { project, name: file.name, size: file.size, sha256, source, label: options.label };
        const started = await startUpload({ ...ask, newProject: options.newProject }, headers);
        rememberUpload({
          project,
          id: started.id,
          name: file.name,
          size: file.size,
          lastModified: file.lastModified,
          sha256,
          source: started.source,
          label: options.label,
          startedAt: Date.now(),
        });
        onStarted?.(started, options);
        const ranges = partRanges(started.size, started.part_size);
        const sent: Record<number, number> = {};
        for (const number of started.done) {
          const range = ranges.get(number);
          if (range) sent[number] = range[1] - range[0];
        }
        setState({ phase: "sending", file, started, sent: { ...sent } });
        await sendMissingParts(
          project,
          file,
          started,
          headers,
          (number, bytes) => {
            sent[number] = bytes;
            setState({ phase: "sending", file, started, sent: { ...sent } });
          },
          own.signal,
        );
        setState({ phase: "completing", file, started });
        const upload = await completeUpload(project, started.id, headers);
        forgetUpload(project, started.id);
        setState({ phase: "done", file, upload });
        void queryClient.invalidateQueries({ queryKey: uploadKeys.uploads(project) });
        onDone?.(upload, options);
      } catch (error) {
        if (controller.current === own) own.abort(); // the other parts in flight stop with it
        if (aborted(error)) setState({ phase: "cancelled", file });
        else setState({ phase: "failed", file, error: toInfo(error) });
        void queryClient.invalidateQueries({ queryKey: uploadKeys.uploads(project) });
      }
    },
    [onStarted, onDone, queryClient],
  );

  const cancel = useCallback(() => controller.current?.abort(), []);
  const reset = useCallback(() => setState({ phase: "idle" }), []);

  const busy = isBusy(state);
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);
  useEffect(() => () => controller.current?.abort(), []);

  return { state, start, cancel, reset, busy };
}
