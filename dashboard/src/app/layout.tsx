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

  return (
    <html lang="en">
      <body>
        {email ? <Nav email={email} /> : null}
        <main className="shell">{children}</main>
      </body>
    </html>
  );
}
