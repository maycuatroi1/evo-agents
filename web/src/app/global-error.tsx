"use client";

import "./globals.css";

/**
 * The last resort when the root layout itself fails: no providers, no translations, so both languages inline,
 * English first as everywhere else.
 */
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="en">
      <body className="flex min-h-svh items-center justify-center bg-background px-4 font-sans text-foreground">
        <main className="flex max-w-md flex-col items-center gap-4 text-center">
          <h1 className="text-xl font-semibold">Something unexpected went wrong</h1>
          <p className="text-sm text-muted-foreground" lang="vi">
            Trang gặp lỗi ngoài dự kiến.
          </p>
          {error.digest ? <code className="rounded bg-muted px-2 py-1 font-mono text-xs">{error.digest}</code> : null}
          <button
            type="button"
            onClick={reset}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
