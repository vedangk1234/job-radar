import type { Metadata } from "next";

import { auth, isAuthorised } from "@/auth";
import { Nav } from "@/components/Nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "job-radar",
  description: "Companies, preferences and jobs for the job-radar crawler.",
};

// The chrome shows who is signed in, so it cannot be prerendered at build time.
export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  const email = isAuthorised(session) ? session!.user!.email! : null;

  return (
    <html lang="en">
      <body>
        {email ? <Nav email={email} /> : null}
        <main className="shell">{children}</main>
      </body>
    </html>
  );
}
