"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

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
 */
export function PreferencesEditor({
  initial,
  resume,
  redirectOnSave,
}: {
  initial: Preferences;
  resume: StoredResume | null;
  redirectOnSave?: string;
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
    <>
      <ResumePanel initial={resume} onKeywords={replaceWithSuggestions} />

      <KeywordChips values={keywords} onChange={setKeywords} />

      <div className="panel">
        <h2>Experience</h2>
        <p className="hint">
          A job&rsquo;s description states the minimum it wants. We keep the ones whose minimum
          falls <em>inside</em> your range &mdash; so a posting asking for less than you want is
          dropped too, not just one asking for more. Only the minimum is stored, so a
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

      <CityPicker
        selected={selectedCities}
        onSelectedChange={setSelectedCities}
        allIndia={allIndia}
        onAllIndiaChange={setAllIndia}
      />

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

      <div
        className="panel"
        style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}
      >
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
          disabled={busy || !dirty || overCap || rangeInvalid || noCityAnswer}
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
                  : dirty
                    ? "Save preferences"
                    : "No changes"}
        </button>
        <span className="muted small">
          Last saved {new Date(updatedAt).toLocaleString()}
        </span>
      </div>

      {message ? <p className={`msg ${message.kind}`}>{message.text}</p> : null}
    </>
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
