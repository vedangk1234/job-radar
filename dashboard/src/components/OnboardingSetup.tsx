"use client";

import { useState } from "react";

import type { Preferences, StoredResume } from "@/lib/labels";
import { PreferencesEditor } from "./PreferencesEditor";
import { WatchlistPicker, type PickerCompany } from "./WatchlistPicker";

/**
 * Setup, as one screen: the companies you want, then the filter over them.
 *
 * WHY A WRAPPER EXISTS AT ALL. The two halves of onboarding commit at different
 * moments, and honestly so. Watching a company is a write the instant you press
 * it — it has to be, because creating the pair and suppressing that company's
 * history is one transaction on the server, and there is no staged version of
 * that. The filter is staged and written by one Save, because a half-edited
 * filter must never be what the digest runs on. The resume panel already
 * established that asymmetry on this screen; this adds a second thing on the
 * "commits immediately" side.
 *
 * So the state that has to be shared is one number: how many companies are
 * watched right now. Save is refused while that is zero — by the route, which is
 * where the rule lives, and by the button, which is where a person can see it
 * before pressing. A component that owns both children is the smallest way to
 * carry a number between two client components, and it keeps PreferencesEditor
 * ignorant of the picker: it takes a count, not a watch list.
 *
 * THE ORDER IS DELIBERATE. Companies first, because the filter narrows something
 * — "no keyword matched" and "you watch nobody" are indistinguishable on an empty
 * dashboard, and only one of them is a question about keywords.
 */
export function OnboardingSetup({
  preferences,
  resume,
  catalog,
  initialWatched,
  cap,
}: {
  preferences: Preferences;
  resume: StoredResume | null;
  catalog: PickerCompany[];
  initialWatched: number[];
  cap: number | null;
}) {
  const [watched, setWatched] = useState<number[]>(initialWatched);

  return (
    <>
      <WatchlistPicker
        catalog={catalog}
        initialWatched={initialWatched}
        cap={cap}
        onWatchedChange={setWatched}
      />
      <PreferencesEditor
        initial={preferences}
        resume={resume}
        redirectOnSave="/"
        watchedCount={watched.length}
      />
    </>
  );
}
