"use client";

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";

import type { ExplorerCanvasProps } from "./explorer-canvas-view";

/**
 * The explorer's canvas (./explorer-canvas-view.tsx), loaded in the browser with sigma and graphology once it mounts,
 * so the explorer's page carries neither.
 */
export const ExplorerCanvas = dynamic<ExplorerCanvasProps>(() => import("./explorer-canvas-view"), {
  ssr: false,
  loading: () => <Skeleton className="absolute inset-0 rounded-none opacity-60" />,
});
