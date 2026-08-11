import { requirePage } from "@/lib/guard";
import { accountFootprint } from "@/lib/account";
import { loadPreferences } from "@/lib/preferences";
import { loadResume } from "@/lib/resume";
import { DeleteAccount } from "@/components/DeleteAccount";
import { PreferencesEditor } from "@/components/PreferencesEditor";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * PHASE 2c PUT THE ACCOUNT ON THIS PAGE RATHER THAN ON A NEW ONE. Deleting an
 * account is not a preference, so its honest home is a settings surface that
 * does not exist yet — and inventing one for a single control would mean a fifth
 * nav tab holding one button, which Phase 3 would then have to redesign around.
 * This page is already where a person comes to change what the product does for
 * them, it is already guarded, and the session already carries their address.
 * The section is separated by a rule and sits last; when Phase 3 builds a real
 * account surface it moves as one component and this file loses three lines.
 */
export default async function PreferencesPage({
  searchParams,
}: {
  searchParams: Promise<{ delete?: string }>;
}) {
  // This page had NO guard until Phase 2a — it relied on the middleware matcher
  // alone while reading the database on the server, which is exactly the case
  // the belt-and-braces rule in lib/guard.ts exists for. It also now needs the
  // user id, so the omission could not survive the sub-phase in any event.
  //
  // ONE OF THE THREE HOLES IN SLICE 1b's ONBOARDING GATE, AND THE REASON IS THE
  // SECTION AT THE BOTTOM OF THIS FILE. Deleting an account is the DPDP right to
  // erasure and it lives here; a person who signed up, thought better of it and
  // wants their rows gone must not be told to finish setting up their filter
  // first. It opens no way round the gate — onboarding_completed is set by a
  // save, and a save here is the same editor writing the same row through the
  // same statement /onboarding uses.
  const { userId, email } = await requirePage({ allowUnfinishedOnboarding: true });
  const [preferences, resume, footprint, params] = await Promise.all([
    loadPreferences(userId),
    // The row only — the file itself is never read here. A page render must not
    // depend on a storage round-trip, and the panel needs a name and a date
    // rather than bytes; opening the file is a deliberate click that mints its
    // own short-lived URL. See api/resume/download.
    loadResume(userId),
    accountFootprint(userId),
    searchParams,
  ]);

  return (
    <>
      <h1>Preferences</h1>
      <p className="lede">
        The single row that decides what reaches your inbox. The jobs table stores everything,
        every city, unfiltered; this is the only thing that narrows it. The crawler reads it on
        every digest, so a save here is live on the next run — no redeploy, no commit. Which
        companies it narrows is the other half, and that lives on the Watchlist tab.
      </p>
      {/*
        watchedCount comes off the footprint that is already being loaded for the
        delete section below — `accountFootprint().companies` IS the count of
        user_companies rows — so the fifth save gate costs this page no query at
        all. It is passed here as well as on /onboarding because the rule is
        enforced by the route for every caller, and a Save that is refused by the
        server while the button looks willing is the failure the other four gates
        already avoid.
      */}
      <PreferencesEditor
        initial={preferences}
        resume={resume}
        watchedCount={footprint.companies}
      />

      <hr className="rule" />

      <DeleteAccount email={email} footprint={footprint} notice={params.delete} />
    </>
  );
}
