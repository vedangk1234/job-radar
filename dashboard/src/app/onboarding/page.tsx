import { redirect } from "next/navigation";

import { requirePage } from "@/lib/guard";
import { loadPreferences } from "@/lib/preferences";
import { loadResume } from "@/lib/resume";
import { PreferencesEditor } from "@/components/PreferencesEditor";

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
 */
export default async function OnboardingPage() {
  // The one page allowed past its own gate — see the enumerated holes in
  // lib/guard.requirePage. Gated, it would redirect to itself forever.
  const { userId } = await requirePage({ allowUnfinishedOnboarding: true });

  const [preferences, resume] = await Promise.all([loadPreferences(userId), loadResume(userId)]);

  if (preferences.onboarding_completed) redirect("/");

  return (
    <>
      <h1>Set up your filter</h1>
      <p className="lede">
        One thing decides what reaches your inbox: the keywords below. A job title has to
        contain at least one of them, so this list is yours to write — nothing has been chosen
        for you. Upload your resume and we will read it once and suggest some, or type your own.
        Save when it looks right and the dashboard opens; you can change any of this later.
      </p>
      <PreferencesEditor initial={preferences} resume={resume} redirectOnSave="/" />
    </>
  );
}
