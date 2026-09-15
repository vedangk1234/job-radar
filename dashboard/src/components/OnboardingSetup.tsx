"use client";

import type { CompanyCard } from "@/lib/companies";
import type { Preferences, StoredResume } from "@/lib/labels";
import { CompanyManager } from "./CompanyManager";
import { FormSection, PreferencesEditor } from "./PreferencesEditor";

/**
 * Setup, as one screen: the companies you want, then the filter over them.
 *
 * WHY A WRAPPER EXISTS AT ALL. The two halves of onboarding commit at different
 * moments, and honestly so. Tracking a company (companies.tracked, the shared
 * crawl list) is a write the instant you press it — the CompanyManager toggle
 * PATCHes straight through, and there is no staged version of it. The filter is
 * staged and written by one Save, because a half-edited filter must never be what
 * the digest runs on. The resume panel already established that asymmetry on this
 * screen; the company picker is a second thing on the "commits immediately" side.
 *
 * IT USED TO BE THE PER-USER WATCH PICKER (user_companies, cap 5); the
 * crawl-selection slice replaced it with the crawl picker so signup has one
 * company concept. /watchlist still carries the old per-user pinning for anyone
 * who wants it.
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
  cards,
  trackedCount,
  cap,
}: {
  preferences: Preferences;
  resume: StoredResume | null;
  cards: CompanyCard[];
  trackedCount: number;
  cap: number | null;
}) {
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
          title="Companies to crawl"
          lede="Optional. Pick which company boards we fetch every hour — search, then tap to add. Your filter above is matched against whatever is crawled. You can skip this and add companies later from the Companies tab; sort by Most relevant to find boards with jobs that fit you."
        >
          <CompanyManager initial={cards} cap={cap} trackedCount={trackedCount} />
        </FormSection>
      }
    />
  );
}
