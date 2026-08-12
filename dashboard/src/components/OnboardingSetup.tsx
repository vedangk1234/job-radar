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
 * IT USED TO SHARE ONE NUMBER BETWEEN THE TWO — how many companies were watched —
 * because Save was refused while that was zero, by the route and by the button
 * alike. The filter-first slice removed the refusal: an account watching nothing
 * matches the whole catalog now, so watching none is not a missing answer. The
 * count is still held here for the section's own "3 of 5" tag, which is the picker
 * describing itself rather than a gate.
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
 * immediately; only its position on the page moved. (It reported a suppression
 * count too, until watching a company stopped being what withheld its history —
 * see lib/watchlist.watchCompany.)
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
      /*
       * EXCLUSIONS AND MAX LEVEL ARE NOT ON THIS SCREEN, and they are not dropped
       * from the save either — see the prop's own comment in PreferencesEditor.
       * The editor still holds this row's values and still sends them, so a brand
       * new account writes back the empty exclusions list and the max_level 2 it
       * was seeded with, untouched. Both are editable on /preferences, which is
       * now the only place they are.
       */
      fineTuning={false}
      saveLabel="Finish setup"
      doneCopy={{
        title: "You're all set",
        body:
          "Your filter is saved. We match it against every board we crawl and email you the " +
          "moment a new role fits — jobs already on the board are on your dashboard now.",
        action: "Go to dashboard",
      }}
      extraSections={
        <FormSection
          id="companies"
          title="Companies to watch"
          tag={cap === null ? undefined : `${watched.length} of ${cap}`}
          lede="Optional. Your filter above is matched against every board we crawl, so you will hear about matching jobs whether or not you pin anything here. Pinning is for keeping an eye on the companies you care about most."
        >
          <WatchlistPicker
            catalog={catalog}
            initialWatched={initialWatched}
            cap={cap}
            onWatchedChange={setWatched}
            variant="search"
          />
        </FormSection>
      }
    />
  );
}
