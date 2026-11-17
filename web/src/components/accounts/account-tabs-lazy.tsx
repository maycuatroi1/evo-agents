"use client";

import dynamic from "next/dynamic";

import { TableSkeleton } from "@/components/states/states";

// The tabs after Providers load when first opened: the page's first load carries the provider cards only.
const loading = () => <TableSkeleton rows={3} />;

export const LazyModelsTab = dynamic(() => import("./models-tab").then((module) => module.ModelsTab), { ssr: false, loading });
export const LazyServicesTab = dynamic(() => import("./account-tabs").then((module) => module.ServicesTab), { ssr: false, loading });
export const LazyPolicies = dynamic(() => import("./account-tabs").then((module) => module.Policies), { ssr: false, loading });
export const LazyAccountSharing = dynamic(() => import("./account-sharing").then((module) => module.AccountSharing), { ssr: false, loading });
// The accounts shared with the visitor, under the provider cards: nothing shows while it loads, since none may be shared.
export const LazySharedWithYou = dynamic(() => import("./shared-with-you").then((module) => module.SharedWithYou), { ssr: false, loading: () => null });
