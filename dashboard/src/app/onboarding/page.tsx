import { redirect } from "next/navigation";

import { listCompanies } from "@/lib/companies";
import { requirePage } from "@/lib/guard";
import { loadPreferences } from "@/lib/preferences";
import { loadResume } from "@/lib/resume";
import { watchlistState } from "@/lib/watchlist";
import { OnboardingSetup } from "@/components/OnboardingSetup";

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
 * THE WATCH LIST IS PART OF SETUP, SINCE THE PICKER SLICE, AND IT IS THE HALF
 * THAT WAS MISSING. Everything this screen collected until now narrowed something
 * — but the digest's first question is not "does this title match" and never was:
 * it is "is this job at a company you asked about". A person who finished
 * onboarding watched nothing, so the answer was no for every job on the board and
 * their digest was empty forever, with a perfectly good filter on the row and a
 * dashboard showing the whole shared catalog as if it were theirs. The landing
 * page says "tell us the companies you'd like to watch"; this is where they do.
 *
 * SO THE SAVE NOW REQUIRES ONE, and that is a fail-closed choice made on the same
 * argument as the empty city selection rather than a new kind of rule: an account
 * that watches nothing matches nothing, and being released onto an empty
 * dashboard with no explanation is the failure the other four required answers
 * exist to prevent. The refusal lives in PUT /api/preferences; the button says so
 * first. See components/OnboardingSetup for why the picker commits immediately
 * while the filter waits for Save.
 */
export default async function OnboardingPage() {
  // The one page allowed past its own gate — see the enumerated holes in
  // lib/guard.requirePage. Gated, it would redirect to itself forever.
  const { userId } = await requirePage({ allowUnfinishedOnboarding: true });

  const [preferences, resume, catalog, watchlist] = await Promise.all([
    loadPreferences(userId),
    loadResume(userId),
    listCompanies(),
    watchlistState(userId),
  ]);

  if (preferences.onboarding_completed) redirect("/");

  return (
    <>
      <h1>Set up your radar</h1>
      <p className="lede">
        Two things decide what reaches your inbox. The filter — a job title has to contain one of
        your keywords, the years its description asks for have to fall inside your range, and its
        city and arrangement have to be ones you would take. And the companies you watch: we
        crawl every board in the catalog hourly, and you hear about the ones you pick. Both are
        yours to write and nothing has been chosen for you. Upload your resume and we will read
        it once and suggest some keywords, or type your own. It is one page — answer what is
        below, and the bar at the bottom says what is still missing. Save when it looks right and
        the dashboard opens; you can change any of this later.
      </p>
      <OnboardingSetup
        preferences={preferences}
        resume={resume}
        catalog={catalog}
        initialWatched={watchlist.companyIds}
        cap={watchlist.cap}
      />
    </>
  );
}
