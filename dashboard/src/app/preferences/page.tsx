import { requirePage } from "@/lib/guard";
import { loadPreferences } from "@/lib/preferences";
import { PreferencesEditor } from "@/components/PreferencesEditor";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export default async function PreferencesPage() {
  // This page had NO guard until Phase 2a — it relied on the middleware matcher
  // alone while reading the database on the server, which is exactly the case
  // the belt-and-braces rule in lib/guard.ts exists for. It also now needs the
  // user id, so the omission could not survive the sub-phase in any event.
  const { userId } = await requirePage();
  const preferences = await loadPreferences(userId);

  return (
    <>
      <h1>Preferences</h1>
      <p className="lede">
        The single row that decides what reaches your inbox. The jobs table stores everything,
        every city, unfiltered; this is the only thing that narrows it. The crawler reads it on
        every digest, so a save here is live on the next run — no redeploy, no commit.
      </p>
      <PreferencesEditor initial={preferences} />
    </>
  );
}
