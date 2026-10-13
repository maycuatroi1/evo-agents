import type { PlanStep } from "@/lib/plans";

/** The Graph view's frame: its height is fixed, so the skeleton, the error and the graph take the same room. */
export const GRAPH_FRAME = "flex h-[32rem] min-w-0 flex-col overflow-hidden rounded-md border bg-card md:h-[40rem]";

export type StepGraphProps = {
  project: string;
  planId: string;
  steps: PlanStep[];
  /** The plan's `acceptance` section as the plan holds it, for the criteria a step names in its inspector. */
  acceptance: unknown;
  /** The keys the search and the repo filter keep; the graph dims the other steps instead of hiding them. */
  matching: ReadonlySet<string> | null;
  /** Switches the section to Board, offered when the graph cannot load. */
  onBoard: () => void;
};
