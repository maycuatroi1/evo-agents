"use client";

import dynamic from "next/dynamic";

/*
 * The probe UI loads on demand (next/dynamic): the Curator's pages and the Inbox share one chunk with the Curator page
 * and the proposal view, and most projects have no probe, so the report and the card stay out of their First Load JS.
 */
export const ProbeReport = dynamic(() => import("./probe-report").then((module) => module.ProbeReport), { ssr: false });
export const ProbeCard = dynamic(() => import("./probe-card").then((module) => module.ProbeCard), { ssr: false });
export const FixOfNote = dynamic(() => import("./probe-card").then((module) => module.FixOfNote), { ssr: false });
