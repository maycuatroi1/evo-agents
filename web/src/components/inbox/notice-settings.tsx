"use client";

import { BellRing } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";

/** The dialog, loaded when the button is first pressed (notice-settings-dialog.tsx). */
const NoticeSettingsDialog = dynamic(() => import("./notice-settings-dialog").then((module) => module.NoticeSettingsDialog));

/** Notices in the Inbox's header: a `secondary` button that opens the dialog of the notices a member may turn off. */
export function NoticeSettingsButton() {
  const t = useTranslations("inbox.settings");
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  if (open && !loaded) setLoaded(true);
  return (
    <>
      <Button type="button" variant="outline" onClick={() => setOpen(true)} aria-haspopup="dialog" data-testid="inbox-notice-settings">
        <BellRing aria-hidden="true" />
        {t("button")}
      </Button>
      {loaded ? <NoticeSettingsDialog open={open} onOpenChange={setOpen} /> : null}
    </>
  );
}
