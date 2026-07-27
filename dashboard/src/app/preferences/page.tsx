import { loadPreferences } from "@/lib/preferences";
import { PreferencesEditor } from "@/components/PreferencesEditor";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export default async function PreferencesPage() {
  const preferences = await loadPreferences();

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
