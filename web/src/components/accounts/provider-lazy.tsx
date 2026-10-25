"use client";

import dynamic from "next/dynamic";

// The catalog and the provider form, and Change key and Delete of a provider account, load the first time one opens,
// so they stay out of the page's first load; the buttons that open them ask for them as the pointer or focus nears.
const loadDialog = () => import("./provider-dialog");
const loadKey = () => import("./provider-key-dialog");
export const LazyProviderDialog = dynamic(() => loadDialog().then((module) => module.ProviderDialog), { ssr: false });
export const LazyProviderKeyDialog = dynamic(() => loadKey().then((module) => module.ProviderKeyDialog), { ssr: false });
export const LazyProviderDeleteDialog = dynamic(() => loadKey().then((module) => module.ProviderDeleteDialog), { ssr: false });
export const preloadProvider = () => void loadDialog();
