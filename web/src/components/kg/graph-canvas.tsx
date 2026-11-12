"use client";

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";

import type { ExplorerLayout } from "./explorer-layout";
import type { GraphCanvasProps } from "./graph-canvas-view";

/** A node's page offers the explorer's layouts: rings by steps from the node, the force layout, a tree from the node. */
export const LAYOUTS: readonly ExplorerLayout[] = ["radial", "force", "tree"];

/**
 * A node's neighbourhood on its page (./graph-canvas-view.tsx), loaded in the browser with sigma and graphology once
 * it mounts, so the node's page carries neither.
 */
export const GraphCanvas = dynamic<GraphCanvasProps>(() => import("./graph-canvas-view"), {
  ssr: false,
  loading: () => <Skeleton className="h-[28rem] w-full rounded-md xl:h-[32rem]" />,
});
