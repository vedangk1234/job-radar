"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

// From lib/labels, never lib/preferences: the latter imports postgres.js, and
// importing a VALUE from it here would pull the driver into the client bundle.
import {
  KEYWORD_CAP,
  LEVEL_LABELS,
  SELECTABLE_CITY_LABELS,
  type Preferences,
  type StoredResume,
} from "@/lib/labels";
import { CityPicker } from "./CityPicker";
import { SalaryBandPicker } from "./SalaryBandPicker";
import { WorkModePicker } from "./WorkModePicker";
import { KeywordChips } from "./KeywordChips";
import { ResumePanel } from "./ResumePanel";

/** The 24 labels the picker offers, as a set — see CityPicker for the two it omits. */
const SELECTABLE = new Set(SELECTABLE_CITY_LABELS);

/**
 * Editor for the one preferences row. Replaces editing the filter by hand in
 * a script or a psql session.
 *
 * Everything is staged locally and written by one Save, rather than each chip
 * being its own PUT. With a single row that means the thing on screen is always
 * a coherent filter: you can drop a keyword and add its replacement without the
 * digest briefly running against the half-edited version in between.
 *
 * THE RESUME IS THE ONE THING THAT DOES NOT WAIT FOR SAVE, and the asymmetry is
 * honest rather than accidental. Uploading a file IS the act of storing it —
 * there is no version of "staged locally" for a 300 KB PDF that has to reach a
 * bucket. What the upload produces here is a list of SUGGESTED keywords, and
 * those are staged like everything else: they land in the chips, a person edits
 * them, and Save writes them. So the screen has two commitments with two
 * different moments, and each says which it is.
 *
 * KEYWORDS MOVED OUT OF THE GENERIC ChipList IN SLICE 1. There is exactly one
 * keyword editor on this page, because two controls writing preferences.keywords
 * with different caps is a split-brain waiting to be discovered by whichever one
 * saves second.
 *
 * redirectOnSave IS THE WHOLE OF WHAT SLICE 1b ADDED, and it is one optional
 * string rather than a mode. /onboarding passes "/" because a successful save
 * there is the act that sets onboarding_completed, which is the act that opens
 * the rest of the app — leaving somebody on the setup screen afterwards would
 * mean the gate had lifted with nothing on the page saying so. /preferences
 * passes nothing and is unchanged: navigating away from a settings screen when
 * you press Save is how an edit you meant to keep tweaking gets lost.
 *
 * A path, not a callback, because this is a client component rendered by a
 * server one and a function is not serialisable across that boundary.
 *
 * THE ONBOARDING-FORM SLICE CHANGED THE LAYOUT AND NOTHING ELSE, which is worth
 * stating plainly because the diff is large. Every field, every validation rule,
 * every gate on the Save button and the whole of save() are byte-for-byte what
 * they were: the same PUT, the same body, the same disabled expression, the same
 * label chain. What changed is that the panels are grouped into named sections in
 * a deliberate order, and that the Save button lives in a bar that stays on screen
 * while a person scrolls — because this form is now six sections long and a button
 * at the bottom of it is a button nobody can see the state of.
 */
export function PreferencesEditor({
  initial,
  resume,
  redirectOnSave,
  watchedCount,
  extraSections,
  watchlistHref = "/watchlist",
}: {
  initial: Preferences;
  resume: StoredResume | null;
  redirectOnSave?: string;
  /**
   * HOW MANY COMPANIES THIS ACCOUNT WATCHES, since the picker slice — the fifth
   * member of the family this button already gates on (city answer, keywords,
   * experience range, work arrangement).
   *
   * It belongs here for the same reason those four do: PUT /api/preferences
   * refuses a save that would leave the account matching nothing, and this is the
   * screen saying why before somebody presses. An empty watch list matches
   * nothing by construction rather than by rule — the digest queue's first clause
   * is `EXISTS (user_companies …)` — so an onboarded account without one gets an
   * empty digest and a blank Home with nothing on screen explaining it.
   *
   * A COUNT, NOT THE LIST, and optional. This editor has no business knowing what
   * a watch list is; it needs to know whether there is one. Undefined means "the
   * caller is not telling me", and the gate is then not applied — the route still
   * refuses, so the rule holds either way and no caller can accidentally weaken
   * it by omission.
   */
  watchedCount?: number;
  /**
   * SECTIONS THIS EDITOR DOES NOT OWN, RENDERED IN ITS STACK JUST ABOVE THE SAVE
   * BAR. /onboarding passes the watch-list picker through here; /preferences
   * passes nothing and is one section shorter.
   *
   * A SLOT RATHER THAN THE PICKER ITSELF, for the reason `watchedCount` is a count
   * rather than a list: this editor has no business knowing what a watch list is.
   * It is a slot rather than the caller simply rendering the picker as a sibling
   * because of where the save bar has to be — a `position: sticky; bottom: 0`
   * element stays pinned only while its own containing block is on screen, so a
   * bar that is not the LAST thing in the form scrolls away halfway down the page,
   * which is the one failure a sticky bar exists to prevent.
   */
  extraSections?: ReactNode;
  /**
   * Where "watch at least one company" points when it is the answer that is
   * missing. /preferences leaves it at the default, because the fix is genuinely
   * on another screen; /onboarding passes the anchor of the section it renders
   * through `extraSections`, because sending somebody to /watchlist mid-setup is a
   * round trip the gate would bounce straight back.
   */
  watchlistHref?: string;
}) {
  const router = useRouter();

  /*
   * THE CITY ANSWER IS TWO FIELDS SINCE SLICE B, and the initial value of the
   * first one carries the migration fallback the matchers carry.
   *
   * `initial.selected_cities` is empty for an account that has not saved since
   * this slice, and its answer is sitting in `initial.cities` — the legacy column
   * — which is exactly what notify.match.Preferences.effective_cities reads. The
   * screen has to agree with the gate about what this person's filter currently
   * IS, or a save would silently rewrite it: render blank, and the first Save
   * writes an empty selection over three cities somebody chose.
   *
   * The two excluded labels are filtered out on the way in for the same reason.
   * A legacy `cities` holding `india-unclear` (the old seeded default did) would
   * otherwise render a checkbox this editor no longer offers, and the route would
   * refuse the save with no control on screen to fix it.
   */
  const [selectedCities, setSelectedCities] = useState<string[]>(() => {
    const stored = initial.selected_cities.length ? initial.selected_cities : initial.cities;
    return stored.filter((c) => SELECTABLE.has(c));
  });
  const [allIndia, setAllIndia] = useState<boolean>(initial.all_india);
  /*
   * NO LEGACY FALLBACK HERE, unlike the city selection above, and the asymmetry
   * is honest rather than an oversight. `selected_cities` needed one because an
   * account's answer might be sitting in the older `cities` column; work_modes
   * has no predecessor to fall back TO. An account that has not saved since this
   * slice renders whatever the column holds — all three for the operator's row
   * (scripts/backfill_onboarding_prefs.py wrote them), empty for a brand-new
   * account, which is exactly the unconfigured state onboarding exists to fill.
   */
  const [workModes, setWorkModes] = useState<string[]>(initial.work_modes ?? []);
  // `?? []` for the reason work_modes has it: a row loaded before the column
  // existed carries undefined, and an empty selection is a legitimate resting
  // state for this field rather than an error — see SalaryBandPicker.
  const [salaryBands, setSalaryBands] = useState<string[]>(initial.salary_bands ?? []);
  const [keywords, setKeywords] = useState<string[]>(initial.keywords);
  const [exclusions, setExclusions] = useState<string[]>(initial.exclusions);
  const [maxLevel, setMaxLevel] = useState<number>(initial.max_level);
  const [updatedAt, setUpdatedAt] = useState<string>(initial.updated_at);

  /*
   * THE EXPERIENCE FIELDS, AND THE ONE PLACE THIS SCREEN SUGGESTS AN ANSWER.
   *
   * The range is held as strings, not numbers, because a number input has to be
   * allowed to be EMPTY while somebody retypes it — `Number("")` is 0, so storing
   * numbers would turn a half-typed field into a silent "0 years" and the Save
   * button would light up for a value nobody chose. Empty stays empty here and the
   * save is blocked until both ends are real.
   *
   * showNoExp DEFAULTS TO ON FOR AN UNCONFIGURED ROW, which is the one place this
   * editor pre-fills anything — so it is worth being explicit about why it does not
   * contradict Slices 1b and 1c. Those removed the OPERATOR'S answers (their
   * keywords, their cities, their exclusions, their max_level) from a new account's
   * row. "Unknown means keep" is not the operator's preference; it is this
   * project's documented, one-directional inclusion bias, argued in notify/match.py
   * and db/schema.sql, and it is what the install has always done. The alternative
   * is a new account whose first digest is empty for a reason nothing on screen
   * explains, since 99% of open postings state no minimum.
   *
   * It is a SUGGESTION, not a smuggled default: the column stays FALSE until a real
   * save. `initial.show_no_experience_jobs || !configured` is the whole of it — an
   * account that has deliberately turned this OFF and saved reads back as
   * configured, so its FALSE is honoured rather than being re-suggested to TRUE on
   * every visit.
   */
  const rangeConfigured = initial.exp_range_low !== null && initial.exp_range_high !== null;
  const [expLow, setExpLow] = useState<string>(
    initial.exp_range_low === null ? "" : String(initial.exp_range_low),
  );
  const [expHigh, setExpHigh] = useState<string>(
    initial.exp_range_high === null ? "" : String(initial.exp_range_high),
  );
  const [yearsHeld, setYearsHeld] = useState<string>(
    initial.years_of_experience_held === null ? "" : String(initial.years_of_experience_held),
  );
  const [showNoExp, setShowNoExp] = useState<boolean>(
    rangeConfigured ? initial.show_no_experience_jobs : true,
  );

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);

  /*
   * `initialSelectedCities` rather than `initial.selected_cities`, because the
   * state above is seeded through the legacy fallback: an account that has not
   * saved since Slice B renders its `cities` in the picker, and comparing that
   * against an empty `selected_cities` would mark a screen nobody touched as
   * dirty. The dirty check has to compare against what was RENDERED.
   */
  const initialSelectedCities = (
    initial.selected_cities.length ? initial.selected_cities : initial.cities
  ).filter((c) => SELECTABLE.has(c));

  const dirty =
    JSON.stringify([
      selectedCities, allIndia, keywords, exclusions, maxLevel, expLow, expHigh, yearsHeld, showNoExp,
      workModes,
      salaryBands,
    ]) !==
    JSON.stringify([
      initialSelectedCities,
      initial.all_india,
      initial.keywords,
      initial.exclusions,
      initial.max_level,
      initial.exp_range_low === null ? "" : String(initial.exp_range_low),
      initial.exp_range_high === null ? "" : String(initial.exp_range_high),
      initial.years_of_experience_held === null ? "" : String(initial.years_of_experience_held),
      rangeConfigured ? initial.show_no_experience_jobs : true,
      initial.work_modes ?? [],
      initial.salary_bands ?? [],
    ]);

  const overCap = keywords.length > KEYWORD_CAP;

  /*
   * The city answer is missing when neither half of it was given. Gated on the
   * button for the same reason `rangeInvalid` is: the route refuses it regardless
   * — that is where the rule lives — and this is about saying why before somebody
   * presses. Note it is NOT "selectedCities is empty": with All over India on, an
   * empty selection is the correct and complete answer.
   */
  const noCityAnswer = !allIndia && !selectedCities.length;

  /*
   * The same rule PUT /api/preferences enforces, restated for the button, exactly
   * as noCityAnswer and rangeInvalid are. The server is where the rule LIVES.
   */
  const noWorkModeAnswer = workModes.length === 0;

  /*
   * And the fifth: an account watching no company matches nothing, whatever else
   * is on this screen. `undefined` means the caller did not say, and an unstated
   * count is not a claim that the list is empty — see the prop's own comment.
   */
  const noCompaniesWatched = watchedCount !== undefined && watchedCount === 0;

  /*
   * The same rule PUT /api/preferences enforces, restated for the button. The
   * server is where the rule LIVES — this is about saying why Save is greyed
   * before somebody presses it, exactly as `overCap` is.
   */
  const lowNum = Number(expLow);
  const highNum = Number(expHigh);
  const rangeBlank = expLow.trim() === "" || expHigh.trim() === "";
  const rangeInvalid =
    rangeBlank ||
    !Number.isInteger(lowNum) ||
    !Number.isInteger(highNum) ||
    lowNum < 0 ||
    highNum > 20 ||
    lowNum > highNum;
  const rangeError = rangeBlank
    ? "Both ends of the range are needed — an unset range matches no job at all."
    : !Number.isInteger(lowNum) || !Number.isInteger(highNum) || lowNum < 0 || highNum > 20
      ? "Use whole years, from 0 to 20."
      : lowNum > highNum
        ? `${lowNum} to ${highNum} is inverted — the low end must not exceed the high end.`
        : null;

  /*
   * WHAT IS STILL MISSING, as a list the save bar can print without a person
   * having to go and find it.
   *
   * DERIVED, NEVER DECIDED. Every entry restates one of the booleans above for a
   * reader — `overCap`, `rangeInvalid`, `noCityAnswer`, `noWorkModeAnswer` and
   * `noCompaniesWatched` are exactly the five terms of the Save button's own
   * `disabled` expression, listed in the order its label chain reports them.
   * Nothing here can block a save the button would have allowed or allow one it
   * would have refused: the bar SAYS, the button DECIDES, and PUT
   * /api/preferences decides again, which is where the rules actually live.
   *
   * THE EMPTY KEYWORD LIST IS THE ONE ENTRY THAT IS NOT A TERM OF THAT EXPRESSION,
   * and it is listed rather than added to it. The route refuses an empty
   * `keywords` — it matches no job at all — so it genuinely blocks a save; it has
   * simply never been part of the button's gate, and adding it there would change
   * what saves rather than what is said. It is listed because on a form this long
   * the Keywords panel's own red message is several screens away from the button,
   * and a person who presses anyway gets precisely the refusal they always got.
   */
  const blockers: { key: string; href: string; text: string }[] = [];
  if (!keywords.length) {
    blockers.push({ key: "keywords", href: "#resume", text: "Add at least one keyword" });
  }
  if (overCap) {
    blockers.push({
      key: "cap",
      href: "#resume",
      text: `Remove ${keywords.length - KEYWORD_CAP} keyword${
        keywords.length - KEYWORD_CAP === 1 ? "" : "s"
      }`,
    });
  }
  if (rangeInvalid) {
    blockers.push({
      key: "range",
      href: "#experience",
      text: rangeBlank ? "Set both ends of the experience range" : "Fix the experience range",
    });
  }
  if (noCityAnswer) {
    blockers.push({ key: "cities", href: "#cities", text: "Pick a city, or choose all of India" });
  }
  if (noWorkModeAnswer) {
    blockers.push({ key: "modes", href: "#work-mode", text: "Pick a work arrangement" });
  }
  if (noCompaniesWatched) {
    blockers.push({ key: "watch", href: watchlistHref, text: "Watch at least one company" });
  }

  /**
   * A parsed resume REPLACES the keyword list outright.
   *
   * REPLACE, NOT MERGE, and it used to be the other way round. Merging read
   * well — nobody who typed six keywords by hand asked for them to be thrown
   * away — but it made the one action people actually perform impossible to
   * perform: uploading a NEW resume because the old one was wrong. The chips
   * are meant to describe the resume on file, and a fold left the previous
   * resume's terms sitting in the list with nothing on screen saying which
   * document they came from. Observed: a replacement resume reporting "13
   * skills found" left the panel showing 25 of 25, twelve of them the old
   * resume's. So the list becomes exactly what was read out of the file that
   * was just uploaded.
   *
   * ONLY ON A SUCCESSFUL PARSE, which is a property of the caller rather than
   * of this function: ResumePanel throws before calling onKeywords if the route
   * refused the file, so a rejected upload cannot clear a list. That ordering
   * is the whole safety argument — a failed replace that wiped the filter would
   * be worse than the merge this replaces.
   *
   * STILL CAPPED, for the same reason as before: suggestions are the one source
   * that can produce more than a person asked for. The server already caps at
   * extraction (api/resume/route.ts passes KEYWORD_CAP to extractSkills), and
   * this restates it rather than trusting it, because the over-cap state is
   * unsaveable and arriving in it through a file upload gives a person no way
   * to see why. Truncating here is not the same act as truncating a list
   * somebody SUBMITTED — see the note in api/preferences/route.ts.
   *
   * A person edits freely afterwards: add, remove and typing are untouched, and
   * nothing is written until Save.
   */
  function replaceWithSuggestions(suggested: string[]) {
    setKeywords(suggested.slice(0, KEYWORD_CAP));
  }

  async function save() {
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch("/api/preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // `cities` is deliberately NOT sent. It is still written — the deployed
          // digest reads it — but the route DERIVES it from the answer below, so
          // there is one city answer on the wire rather than two that could
          // disagree. See api/preferences/route.ts.
          selected_cities: allIndia ? [] : selectedCities,
          all_india: allIndia,
          keywords,
          exclusions,
          max_level: maxLevel,
          // Numbers on the wire, strings in the inputs. The blank case cannot
          // reach here: Save is disabled while the range is invalid, and the
          // route refuses it regardless.
          exp_range_low: Number(expLow),
          exp_range_high: Number(expHigh),
          years_of_experience_held: yearsHeld.trim() === "" ? null : Number(yearsHeld),
          show_no_experience_jobs: showNoExp,
          work_modes: workModes,
          salary_bands: salaryBands,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      setSelectedCities(data.preferences.selected_cities);
      setAllIndia(data.preferences.all_india);
      setKeywords(data.preferences.keywords);
      setExclusions(data.preferences.exclusions);
      setMaxLevel(data.preferences.max_level);
      // Re-read from the row rather than keeping what was typed, exactly as the
      // lists above do: what the database now holds is what the digest will use.
      setExpLow(String(data.preferences.exp_range_low));
      setExpHigh(String(data.preferences.exp_range_high));
      setYearsHeld(
        data.preferences.years_of_experience_held === null
          ? ""
          : String(data.preferences.years_of_experience_held),
      );
      setShowNoExp(data.preferences.show_no_experience_jobs);
      setWorkModes(data.preferences.work_modes);
      setSalaryBands(data.preferences.salary_bands);
      setUpdatedAt(data.preferences.updated_at);
      setMessage({ kind: "ok", text: data.message });

      // ONLY ON A SUCCESSFUL SAVE, and only because the row now says so. The
      // response carries onboarding_completed straight from the UPDATE's
      // RETURNING, so this navigates on what the database recorded rather than
      // on the request having been sent. refresh() first: the destination is a
      // server component behind the same gate, and it has to re-read the flag
      // that just changed rather than serve a cached render from before it did.
      if (redirectOnSave && data.preferences.onboarding_completed) {
        router.refresh();
        router.replace(redirectOnSave);
        return;
      }
    } catch (err) {
      setMessage({ kind: "bad", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="setup-form">
      <FormSection
        id="resume"
        title="Your resume and keywords"
        lede="Upload your resume and we read it once to suggest keywords, or type your own. A job title has to contain at least one of them before anything else is even asked about it."
      >
        <ResumePanel initial={resume} onKeywords={replaceWithSuggestions} />

        <KeywordChips values={keywords} onChange={setKeywords} />
      </FormSection>

      <FormSection
        id="experience"
        title="How much experience you want"
        lede="The years a posting asks for, against the range you are looking in."
      >
        <div className="panel">
          <h2>Experience</h2>
          <p className="hint">
            A job&rsquo;s description states the minimum it wants. We keep the ones whose minimum
            falls <strong>inside</strong> your range &mdash; so a posting asking for less than you
            want is dropped too, not just one asking for more. Only the minimum is stored, so a
            &ldquo;3&ndash;6 years&rdquo; posting counts as 3.
          </p>

          <div className="row" style={{ alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <label className="muted small" htmlFor="exp-low">
              Looking for
            </label>
            <input
              id="exp-low"
              type="number"
              min={0}
              max={20}
              step={1}
              value={expLow}
              onChange={(e) => setExpLow(e.target.value)}
              aria-label="Lowest years of experience to look for"
              style={{ maxWidth: 90 }}
            />
            <span className="muted small">to</span>
            <input
              id="exp-high"
              type="number"
              min={0}
              max={20}
              step={1}
              value={expHigh}
              onChange={(e) => setExpHigh(e.target.value)}
              aria-label="Highest years of experience to look for"
              style={{ maxWidth: 90 }}
            />
            <span className="muted small">years of experience</span>
          </div>
          {rangeError ? <p className="msg bad">{rangeError}</p> : null}

          <div style={{ marginTop: 18 }}>
            <label className="switch">
              <input
                type="checkbox"
                checked={showNoExp}
                onChange={(e) => setShowNoExp(e.target.checked)}
              />
              <span>Show jobs that don&rsquo;t state a minimum</span>
            </label>
            <p className="hint">
              About <strong>99% of open postings state no minimum</strong> &mdash; either the
              description never mentions years, or we could not read it. Leaving this off hides
              almost everything, so it starts on. Turning it off gives you a much shorter list of
              postings that said out loud what they want.
            </p>
          </div>

          <div style={{ marginTop: 18 }}>
            <label className="muted small" htmlFor="years-held">
              Your own experience (optional)
            </label>
            <div className="row" style={{ alignItems: "center", gap: 10 }}>
              <input
                id="years-held"
                type="number"
                min={0}
                max={50}
                step={1}
                value={yearsHeld}
                onChange={(e) => setYearsHeld(e.target.value)}
                placeholder="—"
                aria-label="Years of experience you have"
                style={{ maxWidth: 90 }}
              />
              <span className="muted small">years</span>
            </div>
            <p className="hint">
              Recorded on your profile and shown back to you here. It <strong>never filters
              anything</strong> &mdash; the range above is what decides. Leave it blank if you would
              rather not say.
            </p>
          </div>
        </div>
      </FormSection>

      <FormSection
        id="cities"
        title="Where you want to work"
        lede="Pick the cities you would take a job in, or say the whole country is fine. Jobs outside India are never sent either way."
      >
        <CityPicker
          selected={selectedCities}
          onSelectedChange={setSelectedCities}
          allIndia={allIndia}
          onAllIndiaChange={setAllIndia}
        />
      </FormSection>

      <FormSection
        id="work-mode"
        title="How you want to work"
        lede="On-site, hybrid or fully remote. A fully remote job reaches you from anywhere in the country, whichever cities you picked above."
      >
        <WorkModePicker selected={workModes} onChange={setWorkModes} />
      </FormSection>

      <FormSection
        id="pay"
        title="What you expect to be paid"
        lede="Optional, and the one answer here that narrows almost nothing: hardly any posting on these boards states a salary, and one that does not is shown to you regardless."
      >
        <SalaryBandPicker selected={salaryBands} onChange={setSalaryBands} />
      </FormSection>

      {/*
        THE CALLER'S OWN SECTIONS, and on /onboarding that is the watch-list
        picker. Here rather than as a sibling of this component so that the save
        bar below stays the LAST thing in the form — see the prop's comment.
      */}
      {extraSections}

      <FormSection
        id="tuning"
        title="Fine-tuning"
        lede="Two rules that take jobs back OUT of the list everything above lets in. Both are optional, and both are safe to leave alone."
      >
        <ChipList
          title="Exclusions"
          hint="A title matching ANY of these is dropped, even if a keyword matched. Empty is allowed and means nothing is excluded by title."
          values={exclusions}
          onChange={setExclusions}
          placeholder="e.g. intern"
        />

        <div className="panel">
          <h2>Max level</h2>
          <p className="hint">
            Titles that state a seniority above this are dropped. A title that states no level at
            all counts as 2, so lowering this below 2 filters out most ordinary postings.
          </p>
          <select
            value={maxLevel}
            onChange={(e) => setMaxLevel(Number(e.target.value))}
            style={{ maxWidth: 320 }}
          >
            {Object.entries(LEVEL_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
      </FormSection>

      {/*
        Said in words as well as on the button, because the fix is on ANOTHER
        SCREEN when this editor is /preferences — there is no picker here to
        point at, and a greyed button reading "pick a company to watch" with no
        company control in sight is the kind of dead end this project's other
        blank-answer messages exist to avoid. On /onboarding the picker is a
        section of this same form, and `watchlistHref` points at it instead.
      */}
      {noCompaniesWatched ? (
        <p className="msg bad">
          You are not watching any company, so this filter would match nothing at all — the
          digest asks for jobs at the companies you watch first, and there are none.{" "}
          <Link href={watchlistHref}>Pick some on the Watchlist tab</Link>, then save.
        </p>
      ) : null}

      {/*
        THE SAVE BAR, AND WHY IT IS STICKY.

        This form is six sections and several screens long, so a button at the
        bottom of it is a button whose STATE nobody can see: the whole point of
        greying Save and labelling it with the reason is lost if reading the
        reason means scrolling past everything you were in the middle of
        answering. Pinned to the bottom of the viewport, the answer to "can I
        finish yet, and if not why not" is on screen the entire time.

        `position: sticky; bottom: 0` rather than `fixed`: it is a member of the
        form rather than furniture bolted over it, so it settles at the end of the
        stack instead of hovering above the delete-account section on
        /preferences — and it needs no compensating padding on the page below it.

        THE BUTTON ITSELF IS UNCHANGED, deliberately and to the character: the
        same `disabled` expression, the same label chain in the same order. What
        is new beside it is the list, which restates those same booleans in words
        and points at the section each one lives in. See `blockers`.
      */}
      <div className="save-bar">
        <div className="save-bar-inner">
          <div className="save-state">
            {blockers.length ? (
              <>
                <span className="save-state-title">Still needed</span>
                <ul className="save-blockers">
                  {blockers.map((blocker) => (
                    <li key={blocker.key}>
                      <a href={blocker.href}>{blocker.text}</a>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <span className="save-state-ok">
                {busy
                  ? "Saving…"
                  : dirty
                    ? "Every answer is in. Save when it looks right."
                    : "Everything on this page is saved."}
              </span>
            )}
          </div>

          <div className="save-bar-action">
            <span className="muted small saved-at">
              Last saved {new Date(updatedAt).toLocaleString()}
            </span>
            {/*
              * Disabled over the cap as well as when clean. The server refuses it
              * anyway — that is where the rule lives — so this is about telling
              * somebody why the button is not working before they press it, rather
              * than about enforcement. The label says the number, because a greyed
              * button with no reason is the thing people file bugs about.
              */}
            <button
              className="primary"
              onClick={save}
              disabled={
                busy ||
                !dirty ||
                overCap ||
                rangeInvalid ||
                noCityAnswer ||
                noWorkModeAnswer ||
                noCompaniesWatched
              }
            >
              {busy
                ? "Saving…"
                : overCap
                  ? `Remove ${keywords.length - KEYWORD_CAP} keyword${
                      keywords.length - KEYWORD_CAP === 1 ? "" : "s"
                    } to save`
                  : rangeInvalid
                    ? "Fix the experience range to save"
                    : noCityAnswer
                      ? "Pick a city to save"
                      : noWorkModeAnswer
                        ? "Pick a work arrangement to save"
                        : noCompaniesWatched
                          ? "Pick a company to watch to save"
                      : dirty
                        ? "Save preferences"
                        : "No changes"}
            </button>
          </div>
        </div>

        {message ? <p className={`msg ${message.kind}`}>{message.text}</p> : null}
      </div>
    </div>
  );
}

/**
 * One labelled section of the form: a heading, a line of helper text, and the
 * panels that answer it.
 *
 * PURELY PRESENTATIONAL, and it owns no state at all — which is what keeps this
 * slice a reskin. The panels inside are the same components, holding the same
 * values, writing through the same setters. What the wrapper adds is a name for
 * the group, an id to link to (the save bar's list points at these), and the
 * separation that makes a long single page readable as parts rather than as one
 * undifferentiated stack of cards.
 *
 * THE SECTION TITLE ASKS THE QUESTION AND THE PANEL TITLE NAMES THE FIELD, which
 * is why the two are deliberately not the same words. "Where you want to work"
 * over a panel headed "Cities" reads as a question and its answer; "Cities" over
 * "Cities" would read as a bug.
 */
export function FormSection({
  id,
  title,
  lede,
  children,
}: {
  id: string;
  title: string;
  lede: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="form-section" id={id} aria-labelledby={`${id}-title`}>
      <div className="section-head">
        <h2 className="section-title" id={`${id}-title`}>
          {title}
        </h2>
        <p className="section-lede">{lede}</p>
      </div>
      {children}
    </section>
  );
}

/** Add/remove list of short strings. Used three times, so it is one component. */
function ChipList({
  title,
  hint,
  values,
  onChange,
  placeholder,
  emptyIsError = false,
  emptyMessage,
}: {
  title: string;
  hint: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
  emptyIsError?: boolean;
  emptyMessage?: string;
}) {
  const [draft, setDraft] = useState("");

  function add() {
    // Lowercased and trimmed here as well as on the server, so the chip you see
    // is exactly the string that will be stored and compared against.
    const value = draft.replace(/\s+/g, " ").trim().toLowerCase();
    if (!value) return;
    if (!values.includes(value)) onChange([...values, value]);
    setDraft("");
  }

  return (
    <div className="panel">
      <h2>
        {title} <span className="muted small">({values.length})</span>
      </h2>
      <p className="hint">{hint}</p>

      {values.length ? (
        <ul className="chips">
          {values.map((value) => (
            <li key={value}>
              {value}
              <button
                className="linky"
                onClick={() => onChange(values.filter((v) => v !== value))}
                aria-label={`Remove ${value}`}
                title={`Remove ${value}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className={emptyIsError ? "msg bad" : "chips empty-note"}>
          {emptyMessage ?? "None."}
        </p>
      )}

      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input
          type="text"
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          aria-label={`Add to ${title}`}
        />
        <div className="fixed">
          <button type="submit" disabled={!draft.trim()}>
            Add
          </button>
        </div>
      </form>
    </div>
  );
}
