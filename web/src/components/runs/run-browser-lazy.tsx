"use client";

import dynamic from "next/dynamic";

/** The live browser is a tab of a run started with a browser: its code loads only when the run shows one (next/dynamic). */
export const RunBrowserPanel = dynamic(() => import("./run-browser").then((module) => module.RunBrowserPanel), { ssr: false });
