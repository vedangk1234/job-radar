"use client";

import { useState } from "react";

import type { Preferences, StoredResume } from "@/lib/labels";
import { FormSection, PreferencesEditor } from "./PreferencesEditor";
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
 * THE ORDER CHANGED WITH THE FORM SLICE, AND THE OLD REASON IS WORTH RECORDING
 * BECAUSE IT WAS NOT WRONG. The picker used to be FIRST, on the argument that the
 * filter narrows something and "no keyword matched" is indistinguishable from
 * "you watch nobody" on an empty dashboard. That argument is about which answer
 * is missing when the digest is empty, and it is now answered by the save bar
 * instead — which names every missing answer at once, permanently on screen,
 * whichever order they were asked in.
 *
 * What the old order cost was the opening: the first thing a person saw thirty
 * seconds after signing up was a list of fifty company names, before anything had
 * asked what kind of job they wanted. Resume-first opens with the one thing they
 * already have, and the picker lands after the filter is described — which is
 * also the order the page's own copy has always read in. The picker still commits
 * immediately, still reports its suppression count, and Save is still refused
 * while nothing is watched; only its position on the page moved.
 *
 * IT IS PASSED AS A SECTION OF THE EDITOR'S FORM rather than rendered beside it,
 * so the sticky save bar stays the last element in the stack. See the
 * `extraSections` prop.
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
    <PreferencesEditor
      initial={preferences}
      resume={resume}
      redirectOnSave="/"
      watchedCount={watched.length}
      watchlistHref="#companies"
      extraSections={
        <FormSection
          id="companies"
          title="Companies to watch"
          lede="The first question the digest asks about a job is whose board it came from. Pick at least one company here or nothing above can match, however good the filter is."
        >
          <WatchlistPicker
            catalog={catalog}
            initialWatched={initialWatched}
            cap={cap}
            onWatchedChange={setWatched}
          />
        </FormSection>
      }
    />
  );
}
