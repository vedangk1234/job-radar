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
  const { userId, email } = await requirePage();
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
        every digest, so a save here is live on the next run — no redeploy, no commit.
      </p>
      <PreferencesEditor initial={preferences} resume={resume} />

      <hr className="rule" />

      <DeleteAccount email={email} footprint={footprint} notice={params.delete} />
    </>
  );
}
