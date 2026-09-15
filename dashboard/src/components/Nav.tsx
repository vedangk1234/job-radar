"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { signOutEverywhere } from "@/app/actions";
// From lib/labels, never lib/guard: the latter imports lib/preferences and so
// postgres.js, and importing a VALUE from it here would pull the driver into the
// client bundle. Same rule PreferencesEditor follows. See the constant's comment.
import { ONBOARDING_PATH } from "@/lib/labels";

// Home first because it is the default page and the one worth opening: it
// answers "what should I apply to today". Jobs is the unfiltered reference next
// to it, and the settings tabs sit after both.
//
// Watchlist sits between the two views and the two settings screens, which is
// where it belongs in both directions: it decides what Home and Jobs are lists
// OF, and it is edited about as often as the filter is. It is deliberately not
// called "Companies" — the catalog is shared and this tab is about yours.
const TABS = [
  { href: "/", label: "Home" },
  { href: "/jobs", label: "Jobs" },
  { href: "/companies", label: "Companies" },
  { href: "/watchlist", label: "Watchlist" },
  { href: "/add", label: "Add" },
  { href: "/preferences", label: "Preferences" },
];

/**
 * THE TABS ARE HIDDEN ON /onboarding, AND THAT IS THE WHOLE OF THE CHANGE.
 *
 * CLAUDE.md carried this under *Pending* from Slice 1b: "the nav does not know
 * about the onboarding gate", so during setup the chrome offered Home / Jobs /
 * Add and every one of them bounced straight back here. That was the gate being
 * visible rather than a bug — but a link that undoes itself is a link that reads
 * as a broken product, and the person seeing it is ninety seconds into the app.
 *
 * IT ASKS THE ROUTE, NOT THE DATABASE, and that is what made this a shell change
 * rather than a gate change. The Pending note weighed the two ways of telling the
 * layout — a query per navigation, or threading the flag out of a page into its
 * own layout — and both are answers to "is this account onboarded". That is not
 * the question the chrome has. The question is "is this the setup screen", which
 * usePathname already answers on the client for free, with no round trip and no
 * new read of preferences. The gate is untouched: lib/guard still decides who
 * ends up here, and nothing about this file can let anybody past it.
 *
 * SIGN OUT STAYS, ALWAYS. Hiding navigation during setup is one thing; hiding the
 * way out is another, and an account that cannot leave a screen is a trap. The
 * address and the sign-out form are rendered on both branches for that reason —
 * and /preferences is deliberately NOT in this branch, because deleting an
 * account lives there and it is the second exit a person mid-setup may want. It
 * is reachable from the sign-out row rather than from the tabs.
 */
export function Nav({ email }: { email: string }) {
  const pathname = usePathname();
  const onboarding = pathname === ONBOARDING_PATH;

  return (
    <header className={onboarding ? "top setup" : "top"}>
      <div className="shell">
        <div className="brand">
          job-radar<span>{onboarding ? "setting up" : "dashboard"}</span>
        </div>
        {onboarding ? null : (
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
        )}
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
