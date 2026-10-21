"use client";

import dynamic from "next/dynamic";

// The Add Claude account and Renew token dialog loads the first time it opens, so it stays out of the page's first
// load; the buttons that open it ask for it as soon as the pointer or focus reaches them.
const loadLogin = () => import("./login-dialog");
export const LazyLoginDialog = dynamic(() => loadLogin().then((module) => module.LoginDialog), { ssr: false });
export const preloadLogin = () => void loadLogin();
