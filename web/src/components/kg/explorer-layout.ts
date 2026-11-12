import type { CanvasLayout } from "@/lib/kg/canvas-graph";
import type { Scope } from "@/lib/kg/explorer";

/** The explorer's layouts (lib/kg/canvas-graph.ts lays them out), and those each scope offers. */
export type ExplorerLayout = CanvasLayout;

/** A tree needs a node to grow from, which only a node's scope has. */
export const SCOPE_LAYOUTS: Record<Scope["kind"], readonly ExplorerLayout[]> = {
  overview: ["force", "radial"],
  cluster: ["force", "radial"],
  node: ["force", "radial", "tree"],
};
