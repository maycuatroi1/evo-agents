"use client";

import dynamic from "next/dynamic";

/** The terminal is a tab a member opens: its code loads with the run's page only when the run shows one (next/dynamic). */
export const RunTerminalPanel = dynamic(() => import("./run-terminal").then((module) => module.RunTerminalPanel), { ssr: false });
