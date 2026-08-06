import type { Metadata } from "next";

import { getAuthUser } from "@/lib/supabase/server";
import { Nav } from "@/components/Nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "job-radar",
  description: "Companies, preferences and jobs for the job-radar crawler.",
};

// The chrome shows who is signed in, so it cannot be prerendered at build time.
export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Whether to render the chrome, not whether to allow the request — the gates
  // are middleware.ts and lib/guard.ts. getUser() rather than getSession() all
  // the same: there is one way to ask this question in this app and it is the
  // verifying one, so that nobody has to remember which callers are exempt.
  const user = await getAuthUser();
  const email = user?.email ?? null;

  // Signed out there is exactly one page — the landing page at /signin — and it
  // is full-bleed by design: the hero artwork runs edge to edge. So the shell's
  // gutters and 1080px cap are dropped rather than fought with a viewport-width
  // breakout, which is the trick that leaves a horizontal scrollbar on Windows.
  // The chrome already branches on the session; this is the same branch.
  return (
    <html lang="en">
      <body>
        {email ? <Nav email={email} /> : null}
        <main className={email ? "shell" : "shell-bleed"}>{children}</main>
      </body>
    </html>
  );
}
