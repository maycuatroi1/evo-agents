"use client";

import { Server } from "lucide-react";
import dynamic from "next/dynamic";
import { useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";

// The dialog loads on the first click, so a page that only offers it (the invite page) does not carry it.
const RegisterDialog = dynamic(() => import("./register-dialog").then((module) => module.RegisterDialog), { ssr: false });

/** A button that opens the worker registration dialog, which makes a pairing code; for pages outside /workers. */
export function PairingButton({ label, testId = "pairing-open" }: { label: string; testId?: string }) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  return (
    <>
      <Button
        type="button"
        variant="outline"
        onClick={() => {
          setLoaded(true);
          setOpen(true);
        }}
        data-testid={testId}
      >
        <Server aria-hidden="true" />
        {label}
      </Button>
      {loaded ? <RegisterDialog open={open} onOpenChange={setOpen} onJoined={notify} /> : null}
    </>
  );
}
