"use client";

import dynamic from "next/dynamic";

// The Add, Edit and Renew dialog of an agent's accounts loads the first time it opens, so it stays out of the agent
// page's first load; the buttons that open it ask for it as soon as the pointer or focus reaches them.
const loadDialog = () => import("./agent-account-dialog");
export const LazyAgentAccountDialog = dynamic(() => loadDialog().then((module) => module.AgentAccountDialog), { ssr: false });
export const preloadAgentAccountDialog = () => void loadDialog();
