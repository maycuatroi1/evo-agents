"use client";

import dynamic from "next/dynamic";

import { TableSkeleton } from "@/components/states/states";

/** The account pools group loads after the page around it, as the agents' accounts group does. */
export const LazyAccountPoolsGroup = dynamic(() => import("./account-pools-group").then((module) => module.AccountPoolsGroup), {
  ssr: false,
  loading: () => <TableSkeleton rows={2} />,
});
