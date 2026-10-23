"use client";

import dynamic from "next/dynamic";

// The share dialog loads the first time it opens, so it stays out of the first load of the Accounts and Secrets pages;
// the Share button asks for it as soon as the pointer or focus reaches it.
const loadShare = () => import("./share-dialog");
export const LazyShareDialog = dynamic(() => loadShare().then((module) => module.ShareDialog), { ssr: false });
export const preloadShare = () => void loadShare();
