import { redirect } from "next/navigation";

import { display, mono } from "@/fonts";
import { listCompanyCards, trackedCount } from "@/lib/companies";
import { requirePage } from "@/lib/guard";
import { TRACKED_CAP } from "@/lib/labels";
import { loadPreferences } from "@/lib/preferences";
import { loadResume } from "@/lib/resume";
import { OnboardingSetup } from "@/components/OnboardingSetup";

import "../setup.css";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * SETUP — where lib/guard.requirePage sends an account that has not chosen a
 * filter yet, and the one page that does not enforce that gate on itself.
 *
 * IT IS THE PREFERENCES EDITOR, NOT A COPY OF IT. Same <PreferencesEditor>, same
 * resume panel, same keyword chips, same PUT /api/preferences — because there is
 * exactly one statement in this app that writes preferences.keywords and exactly
 * one place the cap is enforced, and a second editor written for new users would
 * be a second answer to "what is a filter" that drifts from the first the week
 * after it ships.
 *
 * SO WHY A ROUTE OF ITS OWN, RATHER THAN JUST EXEMPTING /preferences AND
 * REDIRECTING THERE. Three things, none of them cosmetic:
 *
 *   - /preferences carries the DELETE ACCOUNT section, behind a rule at the
 *     bottom. Offering erasure — with a footprint naming their row counts — as
 *     the second thing a person sees after signing up is a strange first
 *     impression of a product they have not used yet.
 *   - The exemption becomes a ROUTE rather than a conditional inside a page two
 *     surfaces share. A reader can see which pages opt out by listing them.
 *   - The target and the destination stay distinguishable. This page redirects
 *     to Home once the flag is TRUE; if setup and settings were one URL there
 *     would be nowhere for "done" to point.
 *
 * THE SELF-REDIRECT USES THE ROW IT ALREADY LOADED. onboarding_completed comes
 * back on the same SELECT the editor is filled from, so an onboarded person
 * landing here costs no extra query — and cannot sit on a setup screen for an
 * account that is already set up.
 *
 * WHAT FINISHES IT: redirectOnSave="/". The save writes the row and sets
 * onboarding_completed in the one statement (lib/preferences.savePreferences),
 * and the editor then navigates. /preferences passes no such prop and stays
 * where it is after saving, which is right for a settings screen and wrong here.
 *
 * CHOOSING COMPANIES TO CRAWL IS PART OF SETUP, SINCE THE CRAWL-SELECTION SLICE.
 * The company picker here edits `companies.tracked` — the shared crawl list — so a
 * new user can pick, during setup, which boards the hourly crawl fetches (up to
 * TRACKED_CAP). It is OPTIONAL: the digest and Home are computed from the filter
 * against every crawled board, so skipping it is fine, and the Save is NOT gated
 * on it (that refusal was removed with the filter-first slice). It replaced the
 * per-user "watch up to 5" picker that used to live here — one company concept
 * instead of two. See components/OnboardingSetup for why the picker commits
 * immediately while the filter waits for Save.
 */
export default async function OnboardingPage() {
  // The one page allowed past its own gate — see the enumerated holes in
  // lib/guard.requirePage. Gated, it would redirect to itself forever.
  const { userId } = await requirePage({ allowUnfinishedOnboarding: true });

  const [preferences, resume, cards, tracked] = await Promise.all([
    loadPreferences(userId),
    loadResume(userId),
    listCompanyCards(userId),
    trackedCount(),
  ]);

  if (preferences.onboarding_completed) redirect("/");

  /*
   * THE FONTS ARE THE LANDING PAGE'S, SELF-HOSTED, AND THAT IS THE SAME PRODUCT
   * ARGUMENT MADE TWICE. src/fonts.ts explains it for /signin: a page whose whole
   * promise is that the reader is not the product should not make every visitor's
   * browser announce itself to a Google font CDN before the headline paints. This
   * screen is thirty seconds later in the same session, so it makes no
   * third-party request either — the two .woff2 files are committed, and
   * `next build` needs no network.
   */
  return (
    <div className={`${display.variable} ${mono.variable} setup-page`}>
      <header className="setup-hero">
        <p className="setup-eyebrow">Welcome — one-time setup</p>
        <h1>Set up your filter</h1>
        <p className="lede">
          This decides which jobs reach your inbox. Nothing gets emailed until it is done — and
          you can change any of it later from your dashboard.
        </p>
      </header>
      <OnboardingSetup
        preferences={preferences}
        resume={resume}
        cards={cards}
        trackedCount={tracked}
        cap={TRACKED_CAP}
      />
    </div>
  );
}
