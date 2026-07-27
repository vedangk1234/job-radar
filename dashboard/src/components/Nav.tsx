"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { signOutEverywhere } from "@/app/actions";

// Home first because it is the default page and the one worth opening: it
// answers "what should I apply to today". Jobs is the unfiltered reference next
// to it, and the two settings tabs sit after both.
const TABS = [
  { href: "/", label: "Home" },
  { href: "/jobs", label: "Jobs" },
  { href: "/add", label: "Add" },
  { href: "/preferences", label: "Preferences" },
];

export function Nav({ email }: { email: string }) {
  const pathname = usePathname();

  return (
    <header className="top">
      <div className="shell">
        <div className="brand">
          job-radar<span>dashboard</span>
        </div>
        <nav className="tabs">
          {TABS.map((tab) => (
            <Link
              key={tab.href}
              href={tab.href}
              aria-current={pathname === tab.href ? "page" : undefined}
            >
              {tab.label}
            </Link>
          ))}
        </nav>
        <div className="whoami">
          <span>{email}</span>
          <form action={signOutEverywhere}>
            <button className="small" type="submit">
              Sign out
            </button>
          </form>
        </div>
      </div>
    </header>
  );
}
